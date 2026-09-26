//! Self-contained bundle parsing -- a `mah build --self-contained` output
//! file: a `/bin/sh` stub, then the `mah-vm` binary, then the raw `.mahc`
//! bytes, with a small ASCII header up front describing where everything
//! lives. See the spec's "Bundles" section for the exact layout.
//!
//! ```text
//! #!/bin/sh
//! # mah-bundle v1
//! # vm-version: 0.1.0
//! # vm-target: x86_64-linux
//! # vm-sha256: <64 hex chars>
//! # vm-offset: 000000004096
//! # vm-size: 000001234567
//! # mahc-offset: 000001238663
//! # mahc-size: 000000002345
//! ...shell code (never read by the VM)...
//! <raw vm bytes at vm-offset><raw mahc bytes at mahc-offset>
//! ```
//! All header lines are ASCII, `\n`-terminated; numeric values are
//! zero-padded decimal, always 12 digits. Offsets are absolute byte
//! positions in the file.

const BUNDLE_PREFIX: &[u8] = b"#!/bin/sh\n# mah-bundle v1\n";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BundleInfo {
    pub vm_version: Option<String>,
    pub vm_target: Option<String>,
    pub vm_sha256: Option<String>,
    pub vm_offset: Option<u64>,
    pub vm_size: Option<u64>,
    pub mahc_offset: u64,
    pub mahc_size: u64,
}

fn invalid(why: impl Into<String>) -> String {
    format!("invalid self-contained bundle: {}", why.into())
}

/// If `data` starts with the bundle prefix, parse its `# key: value` header
/// lines (stopping at the first line that doesn't start with `# `),
/// require and bounds-check `mahc-offset`/`mahc-size` (recording
/// `vm-version`/`vm-target`/etc. as available), and return the mahc slice
/// alongside the parsed header. Otherwise `Ok(None)` (a plain, non-bundle
/// file). A malformed bundle header is a format error.
pub fn split(data: &[u8]) -> Result<Option<(BundleInfo, &[u8])>, String> {
    if !data.starts_with(BUNDLE_PREFIX) {
        return Ok(None);
    }
    let mut pos = BUNDLE_PREFIX.len();
    let mut fields: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    loop {
        if pos >= data.len() {
            break;
        }
        // A header line is `# key: value\n`. Stop (without consuming) at
        // the first line that doesn't start with "# ".
        if !data[pos..].starts_with(b"# ") {
            break;
        }
        let rest = &data[pos..];
        let nl = match rest.iter().position(|&b| b == b'\n') {
            Some(n) => n,
            None => return Err(invalid("header line without a newline")),
        };
        let line = &rest[2..nl]; // strip "# "
        let line_str = std::str::from_utf8(line).map_err(|_| invalid("header line is not valid UTF-8"))?;
        let (key, value) = line_str
            .split_once(": ")
            .ok_or_else(|| invalid(format!("malformed header line '{line_str}'")))?;
        fields.insert(key.to_string(), value.to_string());
        pos += nl + 1;
    }

    let parse_u64 = |key: &str| -> Result<Option<u64>, String> {
        match fields.get(key) {
            None => Ok(None),
            Some(v) => v.parse::<u64>().map(Some).map_err(|_| invalid(format!("'{key}' is not a valid number"))),
        }
    };

    let mahc_offset = parse_u64("mahc-offset")?.ok_or_else(|| invalid("missing 'mahc-offset'"))?;
    let mahc_size = parse_u64("mahc-size")?.ok_or_else(|| invalid("missing 'mahc-size'"))?;
    let vm_offset = parse_u64("vm-offset")?;
    let vm_size = parse_u64("vm-size")?;

    if fields.get("vm-version").is_none() {
        return Err(invalid("missing 'vm-version'"));
    }
    if fields.get("vm-target").is_none() {
        return Err(invalid("missing 'vm-target'"));
    }

    let data_len = data.len() as u64;
    let mahc_end = mahc_offset
        .checked_add(mahc_size)
        .ok_or_else(|| invalid("'mahc-offset' + 'mahc-size' overflows"))?;
    if mahc_offset > data_len || mahc_end > data_len {
        return Err(invalid("'mahc-offset'/'mahc-size' out of range of the file"));
    }
    if let (Some(vo), Some(vs)) = (vm_offset, vm_size) {
        let vm_end = vo.checked_add(vs).ok_or_else(|| invalid("'vm-offset' + 'vm-size' overflows"))?;
        if vo > data_len || vm_end > data_len {
            return Err(invalid("'vm-offset'/'vm-size' out of range of the file"));
        }
    }

    let info = BundleInfo {
        vm_version: fields.get("vm-version").cloned(),
        vm_target: fields.get("vm-target").cloned(),
        vm_sha256: fields.get("vm-sha256").cloned(),
        vm_offset,
        vm_size,
        mahc_offset,
        mahc_size,
    };
    let mahc_slice = &data[mahc_offset as usize..mahc_end as usize];
    Ok(Some((info, mahc_slice)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header_line(key: &str, value: &str) -> String {
        format!("# {key}: {value}\n")
    }

    fn make_bundle(mahc: &[u8]) -> Vec<u8> {
        // A placeholder mahc-offset (real value filled in once we know the
        // exact header length, since the offset line's own text is part of
        // what determines that length -- all fields are fixed-width so
        // this converges in one extra pass).
        let build = |mahc_offset: usize| -> Vec<u8> {
            let mut out = Vec::new();
            out.extend_from_slice(BUNDLE_PREFIX);
            out.extend_from_slice(header_line("vm-version", "0.1.0").as_bytes());
            out.extend_from_slice(header_line("vm-target", "x86_64-linux").as_bytes());
            out.extend_from_slice(header_line("vm-sha256", &"a".repeat(64)).as_bytes());
            out.extend_from_slice(header_line("vm-offset", &format!("{:012}", 100)).as_bytes());
            out.extend_from_slice(header_line("vm-size", &format!("{:012}", 10)).as_bytes());
            out.extend_from_slice(header_line("mahc-offset", &format!("{:012}", mahc_offset)).as_bytes());
            out.extend_from_slice(header_line("mahc-size", &format!("{:012}", mahc.len())).as_bytes());
            out.extend_from_slice(b"echo shell stuff\n");
            out
        };
        let header_len = build(0).len();
        let mut out = build(header_len);
        assert_eq!(out.len(), header_len);
        out.extend_from_slice(mahc);
        out
    }

    #[test]
    fn plain_file_is_not_a_bundle() {
        assert_eq!(split(b"MAHC...").unwrap(), None);
    }

    #[test]
    fn parses_a_well_formed_bundle() {
        let mahc = b"hello mahc bytes";
        let data = make_bundle(mahc);
        let (info, slice) = split(&data).unwrap().unwrap();
        assert_eq!(slice, mahc);
        assert_eq!(info.vm_version.as_deref(), Some("0.1.0"));
        assert_eq!(info.vm_target.as_deref(), Some("x86_64-linux"));
    }

    #[test]
    fn missing_mahc_offset_is_invalid() {
        let mut data = BUNDLE_PREFIX.to_vec();
        data.extend_from_slice(header_line("vm-version", "0.1.0").as_bytes());
        data.extend_from_slice(header_line("vm-target", "x86_64-linux").as_bytes());
        data.extend_from_slice(header_line("mahc-size", "000000000005").as_bytes());
        let e = split(&data).unwrap_err();
        assert!(e.contains("missing 'mahc-offset'"), "{e}");
    }

    #[test]
    fn out_of_range_offsets_are_invalid() {
        let mut data = BUNDLE_PREFIX.to_vec();
        data.extend_from_slice(header_line("vm-version", "0.1.0").as_bytes());
        data.extend_from_slice(header_line("vm-target", "x86_64-linux").as_bytes());
        data.extend_from_slice(header_line("mahc-offset", "000000999999").as_bytes());
        data.extend_from_slice(header_line("mahc-size", "000000000005").as_bytes());
        let e = split(&data).unwrap_err();
        assert!(e.contains("out of range"), "{e}");
    }
}
