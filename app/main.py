from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.graph_engine import XMLGraphWorkspace

BASE_DIR = Path(__file__).resolve().parent.parent
XML_DOCS_DIR = BASE_DIR / "xml_documents"
XML_SCHEMAS_DIR = BASE_DIR / "xml_schemas"
STATIC_DIR = Path(__file__).resolve().parent / "static"

XML_DOCS_DIR.mkdir(parents=True, exist_ok=True)
XML_SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
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
    q: str = Query(..., description="Search term title or ICD-10 code"),
    max_results: int = Query(18, ge=1, le=80),
    include_children: bool = Query(True),
):
    ws = get_active_workspace()
    return ws.search_subgraph(
        query=q, max_results=max_results, include_children=include_children
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
