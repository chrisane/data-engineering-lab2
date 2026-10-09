# Retail Data Platform v2

**An end-to-end Data Engineering Learning & Development Project**

## 1. Project Overview

Retail Data Platform v2 is a local-first, enterprise-style data engineering project designed to simulate how a retail organisation collects, processes, validates, integrates, and analyses data from multiple operational and financial systems.

The project uses **Nawa Retail Group**, a fictional Namibian retail organisation, as its business scenario.

The objective is to build a reliable, automated, auditable, and scalable data platform that transforms fragmented source data into trusted, analysis-ready information for business intelligence, financial reporting, and operational decision-making.

The project also serves as a practical learning environment for Python, SQL, ETL/ELT, data architecture, data quality, governance, and analytics engineering.

## 2. Project Objectives

The platform aims to:

1. **Automate data ingestion** from multiple sources, including Excel, CSV, PDF, and eventually email attachments and system APIs.
2. **Implement robust data-quality controls** to detect missing, invalid, duplicate, inconsistent, and non-compliant data.
3. **Provide precise error diagnostics**, identifying the source file, processing stage, worksheet, row, record, field, failed rule, and recommended corrective action.
4. **Preserve data lineage and traceability** from original source documents through ingestion, transformation, warehousing, and reporting.
5. **Implement configurable business rules** that separate validation logic from application code.
6. **Develop reliable ETL/ELT pipelines** with automated processing, exception handling, and safe reprocessing.
7. **Build a structured data warehouse** supporting financial and operational reporting.
8. **Enable reconciliation** between operational systems, source documents, accounting records, and reporting outputs.
9. **Implement monitoring and observability** for pipeline performance, data quality, processing failures, and operational health.
10. **Deliver trusted datasets and semantic models** for Power BI reporting and analytics.
11. **Apply data governance and security principles**, drawing on DAMA-DMBOK and established data engineering practices.
12. **Support maintainability and handover** through documentation, automated testing, configuration management, and version control.

## 3. Business Scenario

Nawa Retail Group operates a fictional multi-site retail business in Namibia.

The organisation includes:

- Head Office
- Central Distribution Centre
- 12 retail branches
- Multiple departments
- Centralised procurement and distribution operations

The platform integrates data across the following business domains:

| Business Domain | Scope |
|---|---|
| Sales | Sales transactions, sales lines, payment methods and customer activity |
| Inventory | Stock movements, stock levels, transfers, adjustments and replenishment |
| Procurement | Purchase orders, goods receipts, supplier invoices and minimum order values |
| Distribution | Branch demand, central warehouse stock and supplier replenishment |
| Supplier Management | Supplier master data, payment terms and rebates |
| Finance | General ledger, chart of accounts, journals, accounts payable and receivable |
| Cash Management | POS shifts, branch cash-ups, cash movements and reconciliation |
| Operating Expenses | Rent, utilities, connectivity, fuel and other operating costs |
| Asset Management | ERP fixed assets, depreciation and local branch asset registers |
| Financial Reporting | Trial balance, income statement, balance sheet and reconciliations |

The synthetic data includes controlled business scenarios and exceptions to support realistic pipeline testing.

## 4. Target Data Architecture

```text
SOURCE SYSTEMS & DOCUMENTS
ERP | POS | Excel | CSV | PDF | Email | APIs
                  |
                  v
            DATA INCOMING
                  |
                  v
           SOURCE DISCOVERY
         Registry & Identification
                  |
                  v
              INGESTION
        File Hashing & Run Tracking
                  |
                  v
               RAW ZONE
        Original Evidence Preserved
                  |
                  v
              VALIDATION
       Structure | Data | Business
                  |
          +-------+-------+
          |               |
         PASS            FAIL
          |               |
          v               v
    TRANSFORMATION     QUARANTINE
          |          Exception Reporting
          v
        STAGING
          |
          v
      DATA WAREHOUSE
     Facts & Dimensions
          |
          v
       DATA MARTS
          |
          v
     SEMANTIC MODELS
          |
          v
        POWER BI

Cross-cutting capabilities:
Monitoring | Logging | Lineage | Security
Governance | Reconciliation | Testing
```

## 5. Core Engineering Capabilities

### 5.1 Source Discovery and Ingestion

- Configurable source registry using YAML
- Automatic file discovery and source identification
- Supported source formats: Excel, CSV and PDF
- File format verification
- SHA-256 hashing and duplicate detection
- Unique ingestion run identifiers
- Preservation of original source files
- Ingestion manifests and processing statuses
- Planned email and API ingestion

### 5.2 Data Validation and Quality

Validation will be implemented at multiple levels:

**Structural validation**
- Workbook readability
- Required worksheets
- Required columns
- Source format and schema compliance

**Data validation**
- Missing mandatory values
- Duplicate business keys
- Data types and numeric ranges
- Invalid dates and values
- Referential integrity

**Business-rule validation**
- Supplier minimum monetary order values
- Inventory availability and stock movements
- Supplier rebate calculations
- Financial posting and balancing rules
- Asset depreciation rules
- Cash-up and POS reconciliation

### 5.3 Error Handling and Diagnostics

A core platform requirement is to identify precisely where and why a processing failure occurred.

Where applicable, diagnostic records will include:

- Ingestion run ID
- Source system or source name
- Original file name and path
- Processing stage
- Worksheet and Excel row number
- Business record identifier
- Field name
- Failed validation rule
- Actual and expected values
- Error type and severity
- Recommended corrective action
- Processing timestamp

Invalid records and files will be handled through controlled exception and quarantine processes rather than silently discarded.

### 5.4 Transformation and Data Warehousing

Planned capabilities include:

- Data cleansing and standardisation
- Master-data integration
- Business-rule-driven transformations
- SQL staging tables
- Dimensional modelling using facts and dimensions
- Star-schema design
- Historical data management
- Incremental loading and safe reprocessing
- Data marts for business reporting

### 5.5 Reconciliation and Financial Integrity

The platform will support reconciliation across operational and financial records, including:

- POS transactions against branch cash-ups
- Sales against inventory movements
- Procurement against goods receipts and supplier invoices
- Inventory transactions against GL postings
- Asset registers against ERP records
- GL debits and credits
- Financial balances and reporting outputs

### 5.6 Monitoring and Observability

Planned monitoring capabilities include:

- Pipeline execution status
- File and record processing counts
- Validation failure rates
- Processing duration
- Duplicate and missing submissions
- Data freshness
- Exception trends
- Automated alerts and actionable error reporting

## 6. Project Structure

```text
data-engineering-lab2/
|
|-- config/                 # Source contracts and business rules
|-- data/
|   |-- incoming/           # Newly received source files
|   |-- raw/                # Preserved source evidence
|   |-- quarantine/         # Rejected or problematic files
|   |-- processed/          # Processed datasets
|   |-- archive/            # Archived data
|
|-- src/
|   |-- ingestion/          # Discovery and ingestion
|   |-- validation/         # Structure and data-quality checks
|   |-- transformation/     # Data transformation
|   |-- warehouse/          # Warehouse loading
|   |-- monitoring/         # Monitoring and diagnostics
|
|-- sql/
|   |-- staging/
|   |-- warehouse/
|   |-- maintenance/
|
|-- scripts/                # Data generation and utilities
|-- tests/                  # Automated and controlled tests
|-- docs/                   # Architecture and technical documentation
|-- logs/                   # Ingestion and validation logs
|-- requirements.txt
|-- README.md
```

## 7. Technology Stack

| Technology | Purpose |
|---|---|
| Python | Ingestion, validation, transformation and automation |
| Pandas | Data processing and analysis |
| OpenPyXL | Excel file processing |
| PyYAML | Source contracts and configurable rules |
| SQL | Staging, transformation and warehouse queries |
| SQLAlchemy / PyODBC | Database integration |
| PyArrow / Parquet | Efficient analytical data storage |
| Pytest | Automated testing |
| Git | Version control |
| Power BI | Semantic modelling, reporting and dashboards |

Additional technologies may be introduced as the platform evolves.

## 8. Getting Started

### Prerequisites

- Python 3.14 or compatible version
- Git
- VS Code or another Python-compatible IDE

### Create and activate the virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### Generate synthetic source data

```powershell
python scripts/generate_all.py
```

Generated files are written to:

`data/incoming/`

### Run source discovery and ingestion

```powershell
python src/ingestion/discover_files.py
```

### Run structural validation

```powershell
python src/validation/validate_structure.py
```

### Run required-value validation

```powershell
python src/validation/validate_data.py
```

**Current limitation:** Validation scripts are still being developed and tested against selected sources. They are not yet fully integrated into an automated, multi-source pipeline.

## 9. Development Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Business model, rules and synthetic data generation | Completed |
| 2 | Source registry, discovery and RAW ingestion | Initial implementation completed |
| 3 | Structural validation and validation logging | Initial implementation completed |
| 4 | Row-level data-quality validation | In progress |
| 5 | Automated validation orchestration and quarantine | Planned |
| 6 | PDF extraction and source reconciliation | Planned |
| 7 | Data transformation and staging | Planned |
| 8 | Data warehouse and dimensional modelling | Planned |
| 9 | Financial and operational reconciliation | Planned |
| 10 | Pipeline scheduling, monitoring and alerting | Planned |
| 11 | Semantic models and Power BI dashboards | Planned |
| 12 | Automated testing, optimisation and production-style hardening | Planned |

## 10. Data Governance and Engineering Principles

The platform follows these guiding principles:

- **Data integrity:** Original source evidence must remain unchanged.
- **Traceability:** Every processed dataset should be traceable to its origin.
- **Auditability:** Pipeline executions, validation results and exceptions must be recorded.
- **Configuration over hardcoding:** Source-specific rules should be defined through maintainable contracts.
- **Idempotency:** Reprocessing should not introduce unintended duplicate data.
- **Separation of concerns:** Ingestion, validation, transformation and loading should have clearly defined responsibilities.
- **Security by design:** Apply appropriate access controls, data protection and secure configuration practices.
- **Data quality by design:** Validate data before it becomes trusted reporting information.
- **Maintainability:** Code and documentation should support straightforward troubleshooting and handover.
- **Business alignment:** Technical controls must support defined business and accounting rules.

## 11. Project Success Criteria

The project will be considered successful when it can:

1. Automatically ingest supported files from multiple registered sources.
2. Detect and classify structural, data-quality and business-rule failures.
3. Identify the exact location and cause of errors, with recommended corrective actions.
4. Preserve original data and provide end-to-end processing lineage.
5. Quarantine invalid inputs without compromising valid data.
6. Reconcile operational and financial data with documented exceptions.
7. Load validated, transformed data into a structured warehouse.
8. Produce trusted Power BI-ready datasets.
9. Support automated, repeatable and monitored pipeline execution.
10. Demonstrate reliable recovery, reprocessing, testing and technical handover.

## 12. Project Scope and Disclaimer

This project is intended for practical learning, experimentation and development of enterprise data engineering capabilities.

All business entities, transactions and financial records are synthetic and do not represent actual organisational information.

The target architecture describes the intended end state. Individual components will be developed, tested and integrated incrementally.

---

**Project:** Retail Data Platform v2  
**Scenario:** Nawa Retail Group  
**Primary focus:** Data Engineering, Data Quality, Governance, Automation and Business Intelligence  
**Development approach:** Local-first, modular and incremental
