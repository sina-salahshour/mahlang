---
title: std:csv
order: 7
section: Text & data
summary: Read and write RFC 4180 CSV, as rows of fields or as records keyed by the header row.
---

# `std:csv`

Comma-separated values, following RFC 4180: a field in double quotes can
hold the delimiter, line breaks, and `""` for a literal quote. Read a file
as **rows** (Vectors of fields) or as **records** (Maps keyed by the
header row), and write either back.

```mah
import csv from "std:csv"

let people = try csv.parse_records("name,age\nal,3\nbo,4\n") else []
print(people.len(), people[1]["name"], people[1]["age"])   # 2 bo 4
```

## Reading rows

`parse(text, delimiter = ",")` gives every row, including the header, as a
`Vector<Vector<String>>`. Quoted fields may contain commas and newlines:

```mah
import csv from "std:csv"

let rows = try csv.parse("city,note\nParis,\"big, old\"\nOslo,\"two\nlines\"\n") else []
print(rows.len(), rows[1][1], rows[2][1].len())     # 3 big, old 9
```

Every field is a **String**; convert as you go with `to_number()`. Lines
may end in `\n` or `\r\n`, and blank lines are skipped. Pass another
`delimiter` for TSV (`"\t"`) or semicolon-separated files.

## Reading records

`parse_records` uses the first row as column names and gives a
`Map<String, String>` per following row. Every row must have as many
fields as the header:

```mah
import csv from "std:csv"

let text = "item;price;qty\npen;1.5;4\npad;3;2\n"
let total = 0
for let row in try csv.parse_records(text, delimiter: ";") else [] {
    total = total + row["price"].to_number() * row["qty"].to_number()
}
print(total)                     # 12
```

To build your own types from records, implement `csv.FromCsvRow`.
`csv.column(row, name)` reads a column and throws a `CsvError` if the file
doesn't have it, rather than giving `none`:

```mah
import csv from "std:csv"

struct Product { name: String, price: Number }

impl csv.FromCsvRow for Product {
    fn from_csv_row(row) {
        Product { name: csv.column(row, "name"), price: csv.column(row, "price").to_number() }
    }
}

let products = try csv.parse_records("name,price\npen,1.5\n").map(fn(r) { Product.from_csv_row(r) }).reduce() else []
print(products[0].name, products[0].price * 2)   # pen 3
```

## Writing

`stringify(rows)` writes rows, quoting only the fields that need it.
Values may be any type: Numbers and Bools are written as text, `none` as an
empty field. `stringify_records(records, columns = none)` writes a header
row (the `columns` you give, or the first record's keys) and one row per
Map; a record missing a column gets an empty field.

```mah
import csv from "std:csv"

print(try csv.stringify([["a", "b,c"], [1, "say \"hi\""]]) else "")
# a,"b,c"
# 1,"say ""hi"""

let recs = [["name": "al", "age": 3], ["name": "bo"]]
print(try csv.stringify_records(recs, columns: ["name", "age"]) else "")
# name,age
# al,3
# bo,
```

## Errors

`csv.CsvError { message, line }` is thrown for a quote that's never
closed, a record with the wrong number of fields, text after a closing
quote, a missing column (from `column`), a record key that isn't a column
(in `stringify_records`), or a bad delimiter (`line` is 0 when it isn't
about a line).

```mah
import csv from "std:csv"

try {
    csv.parse_records("a,b\n1,2,3\n")
} catch {
    e: csv.CsvError => { print(e.line, e.message()) }
}
# 2 this row has 3 fields, but the header has 2 fields at line 2
```

## Reference

| Function | |
|---|---|
| `parse(text, delimiter = ",")` | every row, each a Vector of Strings |
| `parse_records(text, delimiter = ",")` | rows after the first, as Maps keyed by the first |
| `stringify(rows, delimiter = ",")` | rows as CSV text |
| `stringify_records(records, delimiter = ",", columns = none)` | a header row, then one row per Map |
| `column(row, name)` | `row[name]`, throwing if there's no such column |

| Type | |
|---|---|
| `CsvError` | `{ message, line }` |
| `FromCsvRow` | trait with `fn from_csv_row(row: Map<String, String>) -> Self` |
