"""
Staging table definitions derived from the source registry.

Each registered source has a staging table (`staging_table` in the
registry). Its business columns and SQL types come from the source's
validation contract, so the contract stays the single definition:

    numeric, integer: true        -> INT
    numeric                       -> DECIMAL(19, 6)
    extraction type money         -> DECIMAL(19, 2)
    dates / extraction type date  -> DATE
    allowed_values all true/false -> BIT
    identifier-like (pattern, reference or unique key) -> NVARCHAR(50)
    anything else                 -> NVARCHAR(255)
    structure.sql_types           -> explicit override per column

Columns in required_values are NOT NULL; all others allow NULL.
Every staging table also carries the same lineage columns.

Used by:
    scripts/generate_staging_ddl.py  - writes the CREATE TABLE migration
    src/warehouse/staging_loader.py  - knows which columns to insert
    tests                            - prove the database matches the registry
"""

from dataclasses import dataclass

LINEAGE_COLUMNS = [
    ("IngestionRunID", "VARCHAR(40)", False),
    ("SourceFile", "NVARCHAR(500)", False),
    ("SourceRowNumber", "INT", False),
    ("SourceFileHash", "CHAR(64)", False),
]


@dataclass(frozen=True)
class StagingColumn:
    name: str
    sql_type: str
    nullable: bool


@dataclass(frozen=True)
class StagingTable:
    source: str
    schema: str
    table: str
    columns: tuple[StagingColumn, ...]
    business_key: tuple[str, ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]


def source_columns(source_config: dict) -> list[str]:
    """Business columns of a source, in file / extraction order."""

    extraction = source_config.get("extraction")

    if extraction:
        return list(extraction["fields"])

    return list((source_config.get("structure") or {}).get("required_columns") or [])


def sql_type(column: str, source_config: dict) -> str:

    structure = source_config.get("structure") or {}
    rules = source_config.get("data_rules") or {}

    overrides = structure.get("sql_types") or {}

    if column in overrides:
        return overrides[column]

    extraction_field = ((source_config.get("extraction") or {}).get("fields") or {}).get(column)

    if extraction_field:
        kind = extraction_field.get("type", "text")
        if kind == "money":
            return "DECIMAL(19, 2)"
        if kind == "date":
            return "DATE"

    numeric = rules.get("numeric") or {}

    if column in numeric:
        return "INT" if (numeric[column] or {}).get("integer") else "DECIMAL(19, 6)"

    if column in (rules.get("dates") or []):
        return "DATE"

    allowed = (rules.get("allowed_values") or {}).get(column)

    if allowed and all(isinstance(value, bool) for value in allowed):
        return "BIT"

    unique_fields = {field for key in rules.get("unique") or [] for field in ([key] if isinstance(key, str) else key)}

    identifier_like = (
        column in (rules.get("patterns") or {})
        or column in (rules.get("references") or {})
        or column in unique_fields
        or column.endswith("_id")
        or column.endswith("_code")
    )

    return "NVARCHAR(50)" if identifier_like else "NVARCHAR(255)"


def staging_table(source: str, source_config: dict) -> StagingTable:

    qualified = source_config.get("staging_table")

    if not qualified or "." not in qualified:
        raise ValueError(f"Source {source} has no staging_table (schema.table) in the registry.")

    schema, table = qualified.split(".", 1)

    rules = source_config.get("data_rules") or {}
    required = set(rules.get("required_values") or [])

    columns = tuple(
        StagingColumn(name, sql_type(name, source_config), name not in required)
        for name in source_columns(source_config)
    )

    unique = rules.get("unique") or []
    business_key = tuple(([unique[0]] if isinstance(unique[0], str) else unique[0]) if unique else [])

    return StagingTable(source, schema, table, columns, business_key)


def all_staging_tables(registry_config: dict) -> list[StagingTable]:

    return [
        staging_table(source, config)
        for source, config in registry_config["sources"].items()
        if config.get("staging_table")
    ]


def create_table_sql(definition: StagingTable) -> str:
    """Idempotent CREATE TABLE for one staging table."""

    name = definition.qualified_name
    constraint_suffix = f"stg_{definition.table}"

    lines = [f"    StagingID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_{constraint_suffix} PRIMARY KEY,"]

    for column in definition.columns:
        lines.append(f"    [{column.name}] {column.sql_type} {'NULL' if column.nullable else 'NOT NULL'},")

    for column, column_type, _ in LINEAGE_COLUMNS:
        lines.append(f"    {column} {column_type} NOT NULL,")

    lines += [
        f"    LoadedAt DATETIME2(7) NOT NULL CONSTRAINT DF_{constraint_suffix}_LoadedAt DEFAULT SYSUTCDATETIME(),",
        f"    CONSTRAINT CK_{constraint_suffix}_RowNumber CHECK (SourceRowNumber >= 1),",
        f"    CONSTRAINT UQ_{constraint_suffix}_SourceRow UNIQUE (IngestionRunID, SourceFileHash, SourceRowNumber)",
    ]

    body = "\n".join(lines)

    return (
        f"IF OBJECT_ID(N'{name}', N'U') IS NULL\n"
        f"BEGIN\n"
        f"    CREATE TABLE {name} (\n" + "\n".join("    " + line for line in body.splitlines()) + "\n    );\n"
        "END;\nGO\n"
    )
