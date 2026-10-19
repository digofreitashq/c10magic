import csv
import io


NAME_COLUMNS = ["simple name", "nome", "name", "carta", "card", "card name"]
QUANTITY_COLUMNS = ["quantidade", "quantity", "qty", "qtd"]
SET_COLUMNS = ["set code", "edition code", "set_code", "código da edição"]
NUMBER_COLUMNS = ["card number", "collector number", "número", "numero", "cn"]


def find_column(columns, possibilities):
    normalized = {column.strip().lower(): column for column in columns}

    for possibility in possibilities:
        if possibility.lower() in normalized:
            return normalized[possibility.lower()]

    return None


def read_collection(content: str):
    """
    Lê CSV/TSV exportado da LigaMagic.

    Retorna:

        [
            {
                "name": "...",
                "quantity": 4,
                "set_code": "LTR",
                "collector_number": "11",
            },
            ...
        ]
    """

    content = content.lstrip("﻿")

    try:
        delimiter = csv.Sniffer().sniff(
            content[:5000], delimiters=",;\t"
        ).delimiter
    except csv.Error:
        delimiter = ";"

    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

    if not reader.fieldnames:
        raise ValueError("Arquivo sem cabeçalho.")

    name_column = find_column(reader.fieldnames, NAME_COLUMNS)
    quantity_column = find_column(reader.fieldnames, QUANTITY_COLUMNS)
    set_column = find_column(reader.fieldnames, SET_COLUMNS)
    number_column = find_column(reader.fieldnames, NUMBER_COLUMNS)

    if not name_column:
        raise ValueError("Não encontrei a coluna de nome da carta.")

    cards = []

    for row in reader:
        name = (row.get(name_column) or "").strip()

        if not name:
            continue

        quantity = 1

        if quantity_column:
            try:
                quantity = int(row.get(quantity_column) or 1)
            except ValueError:
                quantity = 1

        cards.append({
            "name": name,
            "quantity": quantity,
            "set_code": (row.get(set_column) or "").strip() if set_column else "",
            "collector_number": (
                (row.get(number_column) or "").strip() if number_column else ""
            ),
        })

    return cards


def decode_upload(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError("Não consegui ler o arquivo (codificação desconhecida).")
