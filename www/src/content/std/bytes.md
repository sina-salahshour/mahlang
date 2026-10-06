---
title: std:bytes
order: 9
section: Text & data
summary: Build and decode binary data, from hex, base64 and Vectors of byte values, for the built-in Bytes type.
---

# `std:bytes`

Constructors and decoders for **`Bytes`**, Mah's binary data type: a
growable sequence of byte values (whole Numbers 0 to 255). Bytes are what
binary files ([`std:fs`](/std/fs)), sockets ([`std:socket`](/std/socket))
and HTTP bodies ([`std:http`](/std/http)) read and write.

```mah
import bytes from "std:bytes"

let header = bytes.from_hex("89504e47")        # PNG magic number
let blank = bytes.new(4)                       # four zero bytes
let packet = bytes.concat([header, blank])
print(packet, packet.len())                    # Bytes[89 50 4e 47 00 00 00 00] 8
```

The `Bytes` type itself is built in: its methods (`to_hex()`,
`to_base64()`, `to_text()`, `push`, slicing, ...) need no import. This
module only adds ways to **make** one. The type is covered in full under
[Collections](/docs/collections#bytes); here's the short version.

## Bytes in brief

```mah
let b = "hi".to_bytes()                  # a String's UTF-8 encoding
print(b, b[0], b.len())                  # Bytes[68 69] 104 2
b.push(33)
b[0] = 72
print(b.to_text(), b.to_hex(), b.to_base64())   # some(Hi!) 486921 SGkh
print(b[1..], b == "Hi!".to_bytes())     # Bytes[69 21] true
```

- Indexed and sliced like a Vector (`b[i]` is a Number; a slice is a new
  Bytes), mutable, passed by reference.
- `==` compares **contents** (unlike Vectors). `a + b` concatenates.
- `to_text()` is `some(String)` only if the bytes are valid UTF-8;
  `to_text_lossy()` always gives a String, replacing invalid sequences with
  U+FFFD.

## Making Bytes

```mah
import bytes from "std:bytes"

print(bytes.new(3, 255))                  # Bytes[ff ff ff]
print(bytes.from_vector([1, 2, 254]))     # Bytes[01 02 fe]
print(bytes.from_hex("CAFE"), bytes.from_base64("SGkh").to_text())   # Bytes[ca fe] some(Hi!)
```

## Decoding untrusted text

`from_hex` and `from_base64` throw `bytes.BytesError` for malformed input,
so wrap them when the text comes from outside:

```mah
import bytes from "std:bytes"

fn decode(s: String) -> Bytes {
    try {
        return bytes.from_base64(s)
    } catch {
        e: bytes.BytesError => {
            print(e.kind)                     # invalid_base64
            return bytes.new()
        }
    }
}
print(decode("SGkh").to_text_lossy())         # Hi!
print(decode("not base64!").len())            # 0
```

A bad byte value or size passed to `new`, `from_vector`, `push` or
`b[i] = n` (say, 300) is a programming mistake, so it's a runtime error
rather than a `BytesError`.

## Example: a checksum

Bytes iterate over their Numbers, so ordinary loops and `reduce` work:

```mah
fn checksum(data: Bytes) -> Number {
    let sum = 0
    for let x in data { sum = (sum + x) % 256 }
    sum
}
print(checksum("hello".to_bytes()))      # 20
```

## Reference

| Function | |
|---|---|
| `new(size = 0, fill = 0)` | `size` bytes, each `fill` |
| `from_vector(items)` | from a Vector of Numbers 0 to 255 |
| `from_hex(text)` | an even number of hex digits, either case |
| `from_base64(text)` | standard alphabet, `=` padded, no whitespace |
| `concat(parts)` | a Vector of Bytes joined into one new Bytes |

| `Bytes` method | |
|---|---|
| `len()` | the number of bytes |
| `push(n)`, `pop()` | append a byte / remove the last (`none` if empty) |
| `extend(other)` | append another Bytes |
| `copy()` | a new Bytes with the same contents |
| `index_of(needle)` | `some(i)` of the first occurrence of a Bytes, or `none` |
| `to_vector()` | a Vector of Numbers |
| `to_text()`, `to_text_lossy()` | as UTF-8: `Option<String>` / String |
| `to_hex()`, `to_base64()` | lowercase hex / padded standard base64 |

`bytes.BytesError { kind, description }` has `kind` `"invalid_hex"` or
`"invalid_base64"`. Programs using Bytes need bytecode 1.17.
