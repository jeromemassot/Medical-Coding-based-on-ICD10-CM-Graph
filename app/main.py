from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import google.auth
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel

from app.graph_engine import XMLGraphWorkspace

BASE_DIR = Path(__file__).resolve().parent.parent
XML_DOCS_DIR = BASE_DIR / "xml_documents"
XML_SCHEMAS_DIR = BASE_DIR / "xml_schemas"
SAVED_GRAPHS_DIR = BASE_DIR / "saved_graphs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

XML_DOCS_DIR.mkdir(parents=True, exist_ok=True)
XML_SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
SAVED_GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="XML & XSD Interactive Graph Explorer",
    description="Load an XML document and an XML Schema (XSD), construct a schema-validated Graph representation, and visualize it interactively.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Workspace cache keyed by (xml_filename, xsd_filename)
_WORKSPACES: Dict[Tuple[str, str], XMLGraphWorkspace] = {}
_ACTIVE_KEY: Optional[Tuple[str, str]] = None


def get_or_load_workspace(xml_name: str, xsd_name: str) -> XMLGraphWorkspace:
    global _ACTIVE_KEY
    key = (xml_name, xsd_name)
    if key not in _WORKSPACES:
        xml_path = XML_DOCS_DIR / xml_name
        xsd_path = XML_SCHEMAS_DIR / xsd_name
        if not xml_path.exists():
            raise HTTPException(status_code=404, detail=f"XML file not found: {xml_name}")
        if not xsd_path.exists():
            raise HTTPException(status_code=404, detail=f"XSD file not found: {xsd_name}")
        try:
            _WORKSPACES[key] = XMLGraphWorkspace(xml_path, xsd_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse XML/XSD: {exc}") from exc
    _ACTIVE_KEY = key
    return _WORKSPACES[key]


def get_active_workspace() -> XMLGraphWorkspace:
    global _ACTIVE_KEY
    if _ACTIVE_KEY and _ACTIVE_KEY in _WORKSPACES:
        return _WORKSPACES[_ACTIVE_KEY]

    xml_files = sorted([f.name for f in XML_DOCS_DIR.glob("*.xml")])
    xsd_files = sorted([f.name for f in XML_SCHEMAS_DIR.glob("*.xsd")])
    if not xml_files or not xsd_files:
        raise HTTPException(
            status_code=404,
            detail="No XML or XSD files found in xml_documents/ or xml_schemas/.",
        )
    return get_or_load_workspace(xml_files[0], xsd_files[0])


class LoadRequest(BaseModel):
    xml_filename: str
    xsd_filename: str


@app.on_event("startup")
def startup_load() -> None:
    xml_files = sorted([f.name for f in XML_DOCS_DIR.glob("*.xml")])
    xsd_files = sorted([f.name for f in XML_SCHEMAS_DIR.glob("*.xsd")])
    if xml_files and xsd_files:
        get_or_load_workspace(xml_files[0], xsd_files[0])


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/files")
def list_available_files():
    xml_files = [
        {"name": f.name, "size_bytes": f.stat().st_size}
        for f in sorted(XML_DOCS_DIR.glob("*.xml"))
    ]
    xsd_files = [
        {"name": f.name, "size_bytes": f.stat().st_size}
        for f in sorted(XML_SCHEMAS_DIR.glob("*.xsd"))
    ]
    ws = get_active_workspace()
    return {
        "xml_files": xml_files,
        "xsd_files": xsd_files,
        "active": ws.get_summary(),
    }


@app.post("/api/load")
def load_workspace(req: LoadRequest):
    ws = get_or_load_workspace(req.xml_filename, req.xsd_filename)
    return {
        "status": "ok",
        "active": ws.get_summary(),
        "initial_subgraph": ws.get_initial_subgraph(max_main_terms=10, max_depth=2),
    }


@app.post("/api/upload")
async def upload_files(
    xml_file: Optional[UploadFile] = File(None),
    xsd_file: Optional[UploadFile] = File(None),
):
    ws = get_active_workspace()
    xml_name = ws.xml_path.name
    xsd_name = ws.xsd_path.name

    if xml_file and xml_file.filename:
        dest_xml = XML_DOCS_DIR / Path(xml_file.filename).name
        with dest_xml.open("wb") as f:
            shutil.copyfileobj(xml_file.file, f)
        xml_name = dest_xml.name

    if xsd_file and xsd_file.filename:
        dest_xsd = XML_SCHEMAS_DIR / Path(xsd_file.filename).name
        with dest_xsd.open("wb") as f:
            shutil.copyfileobj(xsd_file.file, f)
        xsd_name = dest_xsd.name

    # Force reload if overwritten
    _WORKSPACES.pop((xml_name, xsd_name), None)
    new_ws = get_or_load_workspace(xml_name, xsd_name)
    return {
        "status": "ok",
        "active": new_ws.get_summary(),
        "initial_subgraph": new_ws.get_initial_subgraph(max_main_terms=10, max_depth=2),
    }


@app.get("/api/schema-graph")
def get_schema_graph():
    ws = get_active_workspace()
    return ws.schema_analyzer.to_cytoscape()


@app.get("/api/subgraph")
def get_subgraph(
    max_main_terms: int = Query(10, ge=1, le=60),
    max_depth: int = Query(2, ge=0, le=5),
):
    ws = get_active_workspace()
    return ws.get_initial_subgraph(
        max_main_terms=max_main_terms, max_depth=max_depth
    )


@app.get("/api/search")
def search_graph(
    q: str = Query(..., description="Semicolon-separated keywords (e.g. 'Abdomen; acute') or ICD-10 code"),
    top_n: Optional[int] = Query(None, ge=1, le=500, description="Top N ranked trajectories to display"),
    max_results: int = Query(15, ge=1, le=500),
    include_children: bool = Query(True),
):
    ws = get_active_workspace()
    n = top_n if top_n is not None else max_results
    return ws.search_subgraph(
        query=q, max_results=n, include_children=include_children
    )


@app.get("/api/expand/{node_id:path}")
def expand_node(
    node_id: str,
    limit: int = Query(25, ge=1, le=100),
):
    ws = get_active_workspace()
    return ws.expand_node(node_id=node_id, limit=limit)


@app.get("/api/node/{node_id:path}")
def get_node_detail(node_id: str):
    ws = get_active_workspace()
    return ws.get_node_detail(node_id=node_id)


class ExtractConditionsRequest(BaseModel):
    medical_note: str


class ExtractedConditionItem(BaseModel):
    condition: str
    keywords: List[str]


class ExtractedConditionsOutput(BaseModel):
    conditions: List[ExtractedConditionItem]


def _get_genai_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        try:
            _, default_project = google.auth.default()
            project = default_project
        except Exception:
            project = None
    project = project or "education-and-tests-422020"
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return genai.Client(vertexai=True, project=project, location=location)


@app.post("/api/extract-conditions")
def extract_conditions_from_note(req: ExtractConditionsRequest) -> Dict[str, Any]:
    note_text = (req.medical_note or "").strip()
    if not note_text:
        raise HTTPException(status_code=400, detail="Medical note text cannot be empty.")

    prompt = (
        "You are a clinical coding expert specializing in the ICD-10-CM Index to Diseases and Injuries.\n"
        "Analyze the following medical note and extract every medical condition, diagnosis, or clinical finding mentioned.\n"
        "For each extracted condition, provide:\n"
        "1. `condition`: The clear clinical name/description of the condition mentioned in the note.\n"
        "2. `keywords`: An ordered list of atomic ICD-10-CM index search keywords associated with this condition "
        "(e.g., main condition term, anatomical site, acuity/chronicity, congenital/acquired modifier, etiology, or subtype) "
        "suitable for searching the ICD-10-CM index graph.\n\n"
        f"Medical Note:\n{note_text}"
    )

    try:
        client = _get_genai_client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractedConditionsOutput,
                temperature=0.1,
            ),
        )
        parsed: Optional[ExtractedConditionsOutput] = getattr(response, "parsed", None)
        if parsed is None and response.text:
            parsed = ExtractedConditionsOutput.model_validate_json(response.text)
        raw_conditions = parsed.conditions if parsed else []
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Gemini (gemini-2.5-flash) extraction failed: {exc}",
        ) from exc

    conditions_payload = []
    for item in raw_conditions:
        cleaned_kws = [k.strip() for k in item.keywords if k and k.strip()]
        conditions_payload.append(
            {
                "condition": item.condition.strip(),
                "keywords": cleaned_kws,
                "keywords_query": "; ".join(cleaned_kws),
            }
        )

    return {
        "model": "gemini-2.5-flash",
        "conditions": conditions_payload,
    }



class SaveGraphRequest(BaseModel):
    scope: str = "canvas"  # "canvas" | "full" | "schema"
    format: str = "pg_json"  # "pg_json" | "pg_jsonl" | "gql_sql"
    directory: Optional[str] = None
    filename: str = "icd10cm_graph.pg.json"
    node_ids: Optional[List[str]] = None
    edge_ids: Optional[List[str]] = None
    search_query: Optional[str] = None


@app.get("/api/fs/dirs")
def list_directories(path: Optional[str] = Query(None, description="Directory path to browse")):
    target = Path(path).expanduser().resolve() if path else SAVED_GRAPHS_DIR.resolve()
    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            target = SAVED_GRAPHS_DIR.resolve()
    if not target.is_dir():
        target = target.parent

    subdirs = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                subdirs.append({"name": entry.name, "path": str(entry.resolve())})
    except PermissionError:
        pass

    parent_path = str(target.parent.resolve()) if target.parent != target else str(target)
    return {
        "current_path": str(target),
        "parent_path": parent_path,
        "default_path": str(SAVED_GRAPHS_DIR.resolve()),
        "workspace_path": str(BASE_DIR.resolve()),
        "directories": subdirs[:100],
    }


@app.post("/api/save-graph")
def save_graph_to_disk(req: SaveGraphRequest):
    ws = get_active_workspace()
    target_dir = Path(req.directory).expanduser().resolve() if req.directory else SAVED_GRAPHS_DIR.resolve()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot create directory '{target_dir}': {exc}") from exc

    safe_name = Path(req.filename.strip() or "graph.pg.json").name
    ext_map = {"pg_json": ".json", "pg_jsonl": ".jsonl", "gql_sql": ".sql"}
    expected_ext = ext_map.get(req.format, ".json")
    if not safe_name.lower().endswith(expected_ext):
        safe_name = f"{safe_name}{expected_ext}"

    output_path = target_dir / safe_name
    try:
        content, meta = ws.serialize_property_graph(
            scope=req.scope,
            fmt=req.format,
            node_ids=req.node_ids,
            edge_ids=req.edge_ids,
            search_query=req.search_query,
        )
        output_path.write_text(content, encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to serialize or write graph file: {exc}") from exc

    return {
        "status": "ok",
        "saved_path": str(output_path),
        "directory": str(target_dir),
        "filename": safe_name,
        "size_bytes": output_path.stat().st_size,
        "format": req.format,
        "metadata": meta,
    }


@app.post("/api/export-graph")
def export_graph_stream(req: SaveGraphRequest):
    ws = get_active_workspace()
    safe_name = Path(req.filename.strip() or "graph.pg.json").name
    ext_map = {"pg_json": ".json", "pg_jsonl": ".jsonl", "gql_sql": ".sql"}
    mime_map = {
        "pg_json": "application/json; charset=utf-8",
        "pg_jsonl": "application/x-ndjson; charset=utf-8",
        "gql_sql": "application/sql; charset=utf-8",
    }
    expected_ext = ext_map.get(req.format, ".json")
    if not safe_name.lower().endswith(expected_ext):
        safe_name = f"{safe_name}{expected_ext}"

    content, _ = ws.serialize_property_graph(
        scope=req.scope,
        fmt=req.format,
        node_ids=req.node_ids,
        edge_ids=req.edge_ids,
        search_query=req.search_query,
    )
    return Response(
        content=content.encode("utf-8"),
        media_type=mime_map.get(req.format, "application/json; charset=utf-8"),
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        },
    )

