#!/usr/bin/env python3

import argparse, csv, io, sys, re
from datetime import datetime
from decimal import Decimal, InvalidOperation

SWEDBANK_COLUMNS = {
    "radnummer": "Radnummer",
    "clearing": "Clearingnummer",
    "konto": "Kontonummer",
    "produkt": "Produkt",
    "valuta": "Valuta",
    "bokfdag": "Bokföringsdag",
    "transdag": "Transaktionsdag",
    "valutadag": "Valutadag",
    "referens": "Referens",
    "beskrivning": "Beskrivning",
    "belopp": "Belopp",
    "saldo": "Bokfört saldo",
}

YNAB_HEADER = ["Date", "Payee", "Memo", "Amount"]


def find_header_index(lines: list[str]) -> int:
    """
    Swedbank lägger ofta en metadata-rad högst upp.
    Vi hittar första raden som ser ut som rubrik med både 'Belopp' och 'Beskrivning'.
    """
    for i, line in enumerate(lines):
        if "Belopp" in line and ("Beskrivning" in line or "Referens" in line):
            return i
    # fallback: anta första raden är rubrik
    return 0


def sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;").delimiter
    except csv.Error:
        # Heuristik på rubrikraden
        header = sample.splitlines()[0]
        return ";" if header.count(";") >= header.count(",") else ","


def parse_decimal(s: str) -> Decimal:
    """
    Tar höjd för både decimalpunkt och decimalkomma samt ev. mellanslag.
    """
    if s is None:
        return Decimal("0")
    s = s.strip().replace("\xa0", " ").replace(" ", "")
    # Om den innehåller både punkt och komma, anta punkt som tusentalsavskiljare och komma som decimal.
    if "," in s and "." in s:
        # Ta bort tusenpunkter
        s = s.replace(".", "")
        s = s.replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Ogiltigt belopp: {s!r}")


def parse_date(s: str) -> str:
    """
    Normalisera datum till YNAB-kompatibelt 'YYYY-MM-DD'.
    Swedbank ger vanligtvis 'YYYY-MM-DD' redan, men vi säkrar upp.
    """
    s = s.strip()
    fmts = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d", "%Y%m%d")
    for f in fmts:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Sista utväg: fånga YYYY-MM-DD med regex
    m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    raise ValueError(f"Ogiltigt datum: {s!r}")


def clean_text(s: str) -> str:
    if s is None:
        return ""
    # Trimma och normalisera whitespace
    return re.sub(r"\s+", " ", s.strip())


def convert_rows(rows, date_field: str, payee_field: str, include_product_in_memo: bool):
    """
    rows: iterable av dict-rader från Swedbank.
    Returnerar lista av YNAB-rader (list[str]).
    """
    out = []
    for r in rows:
        try:
            date_raw = r.get(date_field) or r.get(SWEDBANK_COLUMNS["bokfdag"])
            date = parse_date(date_raw)

            payee = clean_text(r.get(payee_field) or "")
            referens = clean_text(r.get(SWEDBANK_COLUMNS["referens"]))
            beskrivning = clean_text(r.get(SWEDBANK_COLUMNS["beskrivning"]))
            produkt = clean_text(r.get(SWEDBANK_COLUMNS["produkt"]))
            amount = parse_decimal(str(r.get(SWEDBANK_COLUMNS["belopp"], "0")))

            # Memo: visa båda om de inte är identiska
            memo_parts = []
            if referens and referens != beskrivning:
                memo_parts.append(referens)
            if include_product_in_memo and produkt:
                memo_parts.append(f"[{produkt}]")
            memo = " | ".join(p for p in memo_parts if p)

            out.append([date, payee, memo, f"{amount:.2f}"])
        except Exception as e:
            # Skippar helt tomma/trasiga rader (t.ex. saldorader), men varnar på stderr
            print(f"Varning: hoppar över rad p.g.a. fel: {e}", file=sys.stderr)
    return out


def read_text_file_smart(path: str, preferred: str | None = None) -> str:
    """
    Läs textfil och prova några vanliga Svenska bank-encodings.
    Ordning: preferred -> utf-8-sig -> utf-8 -> cp1252 -> iso-8859-1 -> sista utväg (utf-8 med replace).
    """
    encodings = []
    if preferred:
        encodings.append(preferred)
    encodings += ["utf-8-sig", "utf-8", "cp1252", "iso-8859-1"]
    last_err = None
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read()
        except UnicodeDecodeError as e:
            last_err = e
            continue
    # Sista utväg: läs binärt och ersätt trasiga tecken
    with open(path, "rb") as fb:
        return fb.read().decode("utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(
        description="Konvertera Swedbank CSV till YNAB CSV (Date,Payee,Memo,Amount)."
    )
    ap.add_argument("input", help="Swedbank CSV-fil")
    ap.add_argument("-o", "--output", default="-", help="Utfil (default stdout)")
    ap.add_argument("--date-field",
                    default=SWEDBANK_COLUMNS["bokfdag"],
                    choices=[SWEDBANK_COLUMNS["bokfdag"], SWEDBANK_COLUMNS["transdag"], SWEDBANK_COLUMNS["valutadag"]],
                    help="Vilket datumfält ska användas? (default: Bokföringsdag)")
    ap.add_argument("--payee-field",
                    default=SWEDBANK_COLUMNS["beskrivning"],
                    choices=[SWEDBANK_COLUMNS["beskrivning"], SWEDBANK_COLUMNS["referens"]],
                    help="Vilket fält ska bli Payee? (default: Beskrivning)")
    ap.add_argument("--product-in-memo", action="store_true",
                    help="Inkludera Produkt i memo.")
    ap.add_argument("--encoding", help="Tvinga teckenkodning (t.ex. cp1252, utf-8, iso-8859-1).")
    args = ap.parse_args()

    # Läs in filen och hoppa över metadata före rubriken
    raw = read_text_file_smart(args.input, args.encoding)

    lines = raw.splitlines()
    hdr_idx = find_header_index(lines)
    data_str = "\n".join(lines[hdr_idx:])

    # Sniffa delimiter
    delim = sniff_delimiter("\n".join(lines[hdr_idx: hdr_idx + 10] if len(lines) > hdr_idx else lines))
    reader = csv.DictReader(io.StringIO(data_str), delimiter=delim)

    # Verifiera att nödvändiga kolumner finns
    needed = {args.date_field, SWEDBANK_COLUMNS["belopp"]}
    if args.payee_field:
        needed.add(args.payee_field)
    missing = [c for c in needed if c not in reader.fieldnames]
    if missing:
        sys.exit(f"Saknade kolumner i indata: {missing}. Hittade kolumner: {reader.fieldnames}")

    ynab_rows = convert_rows(reader, args.date_field, args.payee_field, args.product_in_memo)

    # Skriv ut YNAB-format
    if args.output == "-" or args.output.lower() == "stdout":
        out_f = sys.stdout
        close = False
    else:
        out_f = open(args.output, "w", encoding="utf-8", newline="")
        close = True

    try:
        w = csv.writer(out_f)
        w.writerow(YNAB_HEADER)
        for row in ynab_rows:
            w.writerow(row)
    finally:
        if close:
            out_f.close()


if __name__ == "__main__":
    main()
