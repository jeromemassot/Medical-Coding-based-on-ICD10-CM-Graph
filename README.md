# XML & XSD Interactive Graph Explorer

An interactive web application that loads an **XML Document** (`xml_documents/icd10cm-index-2027.xml`) and an **XML Schema** (`xml_schemas/icd10cm-index.xsd`), validates the document against the schema, builds a schema-guided **Property & Knowledge Graph**, and visualizes both the **XML Document Content Graph** and the **XSD Schema Meta-Graph** interactively.

## Features

1. **XSD Schema Validation & Meta-Graph Construction (`app/graph_engine.py`)**:
   - Validates the XML document against the XSD schema using `lxml.etree.XMLSchema`.
   - Parses `<xsd:element>`, `<xsd:complexType>`, `<xsd:group>`, `<xsd:attribute>`, and `<xsd:restriction>` definitions to classify XML elements into **Complex Entity Nodes** (`ICD10CM.index`, `letter`, `mainTerm`, `term`), **Inline Modifiers** (`title`, `nemod`, `version`), and **Semantic Reference / Shared Value Nodes** (`code`, `see`, `seeAlso`, `manif`, `seecat`, `subcat`).
   - Builds an interactive **XSD Schema Graph** showing element hierarchy, reusable groups (`termGroup`, `addend`), attributes (`@level`, `@col`, `@isAddenda`), and edge cardinalities (`1..*`, `0..1`, `choice`).

2. **Schema-Guided XML Content Graph**:
   - Indexes all **103,938 graph nodes** from `icd10cm-index-2027.xml` in ~3 seconds.
   - Connects hierarchical parent-child relationships (`ICD10CM.index` → `letter` (`A`–`Z`) → `mainTerm` → recursive `term` levels `1..9`).
   - Connects **shared ICD-10 Code Hubs** (`code:Q27.8`, `code:Q28.1`, etc.) and **cross-reference edges** (`SEE` and `SEE_ALSO`) across branches so users can discover all terms mapping to the same diagnosis code or cross-referenced concept.

3. **Interactive Visualization (`app/static/index.html`)**:
   - Powered by **Cytoscape.js** + **Dagre** hierarchical layout, **CoSE** force-directed layout, **Concentric**, and **Breadthfirst** layouts.
   - **Click** any node to inspect its XML properties, source line number, governing XSD Schema rules, and connected nodes.
   - **Double-click** (or click **+ Expand Connected**) on any node (including shared ICD-10 `code` nodes) to dynamically fetch and merge its connected subgraph onto the canvas.
   - Search by **Term Title** (e.g., `Aberrant`, `Abdomen`) or **ICD-10 Code** (e.g., `Q27.8`), isolate search trajectories on click, or upload custom `.xml` and `.xsd` files.

4. **Save As Property Graph (`PG-JSON`, `PG-JSONL`, & `GoogleSQL DDL`)**:
   - Click **Save As Graph** in the top header or bottom action bar to build a formal **ISO/IEC 39075 GQL Labeled Property Graph** (`networkx.MultiDiGraph`) from the currently displayed canvas subgraph, the full XML document, or the XSD schema meta-graph.
   - Choose the target directory and file name (either via the interactive folder browser saving directly to disk or via the native OS file-save dialog).
   - Supports three graph representations optimized for **LLM comprehension**, **Google Cloud BigQuery Graph**, and **Cloud Spanner Graph**:
     - **Property Graph JSON (`.pg.json` — Default)**: Self-contained document combining an `llm_context` clinical trajectory summary, ready-to-run BigQuery Graph & Spanner Graph `CREATE PROPERTY GRAPH` DDL, and flat primary/foreign-key `nodes` & `edges` collections.
     - **Property Graph JSONL (`.jsonl`)**: Newline-Delimited JSON records (`record_type: "node" | "edge"`) ready for direct `bq load --source_format=NEWLINE_DELIMITED_JSON` and Spanner bulk import.
     - **GoogleSQL Property Graph Script (`.sql`)**: Executable `CREATE TABLE`, batched `INSERT INTO`, and `CREATE OR REPLACE PROPERTY GRAPH` statements.

## Quick Start

```bash
.venv/bin/python run.py
```

Then open **http://127.0.0.1:8000** in your browser.

