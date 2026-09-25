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
   - Search by **Term Title** (e.g., `Aberrant`, `Abdomen`) or **ICD-10 Code** (e.g., `Q27.8`), browse by **Index Letter (`A`–`Z`)**, or upload custom `.xml` and `.xsd` files.

## Quick Start

```bash
.venv/bin/python run.py
```

Then open **http://127.0.0.1:8000** in your browser.
