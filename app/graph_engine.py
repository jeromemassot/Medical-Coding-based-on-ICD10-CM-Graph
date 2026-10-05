from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx
from lxml import etree

XSD_NS = "http://www.w3.org/2001/XMLSchema"
NS_MAP = {"xsd": XSD_NS}


def local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return str(tag)
    if "}" in tag:
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


class XSDSchemaAnalyzer:
    """Parses an XML Schema (.xsd) into a schema dictionary and a Meta-Graph."""

    def __init__(self, xsd_path: Path):
        self.xsd_path = xsd_path
        self.xsd_doc = etree.parse(str(xsd_path))
        self.xml_schema = etree.XMLSchema(self.xsd_doc)
        self.root = self.xsd_doc.getroot()

        self.schema_info: Dict[str, Any] = {
            "filename": xsd_path.name,
            "id": self.root.get("id", ""),
            "version": self.root.get("version", ""),
            "documentation": [],
            "elements": {},
            "groups": {},
        }
        self.schema_graph = nx.DiGraph()
        self._analyze()

    def _analyze(self) -> None:
        # Extract top-level annotations/documentation
        for doc in self.root.findall("xsd:annotation/xsd:documentation", NS_MAP):
            if doc.text and doc.text.strip():
                self.schema_info["documentation"].append(doc.text.strip())

        # 1. Parse model groups (<xsd:group name="...">)
        for group_el in self.root.findall("xsd:group[@name]", NS_MAP):
            gname = group_el.get("name")
            if not gname:
                continue
            children = self._extract_particles(group_el)
            self.schema_info["groups"][gname] = {
                "name": gname,
                "children": children,
            }
            group_node_id = f"xsd_group:{gname}"
            self.schema_graph.add_node(
                group_node_id,
                id=group_node_id,
                label=f"Group: {gname}",
                tag="xsd:group",
                node_type="xsd_group",
                properties={"name": gname, "particle_count": len(children)},
            )

        # 2. Parse element declarations (<xsd:element name="...">) and nested local elements
        for el_decl in self.root.findall(".//xsd:element[@name]", NS_MAP):
            ename = el_decl.get("name")
            if not ename or ename == "letter":
                continue
            etype = el_decl.get("type", "")
            complex_type = el_decl.find("xsd:complexType", NS_MAP)
            is_mixed = False
            attributes = []
            particles = []

            if complex_type is not None:
                is_mixed = complex_type.get("mixed", "false").lower() == "true"
                particles = self._extract_particles(complex_type)
                # Flatten any reference to 'letter' into direct reference to 'mainTerm'
                flattened_particles = []
                for p in particles:
                    if p["kind"] in ("element_ref", "local_element") and p["target"] == "letter":
                        flattened_particles.append(
                            {
                                "kind": "element_ref",
                                "target": "mainTerm",
                                "minOccurs": p.get("minOccurs", "1"),
                                "maxOccurs": p.get("maxOccurs", "unbounded"),
                                "compositor": p.get("compositor", "sequence"),
                            }
                        )
                    else:
                        flattened_particles.append(p)
                particles = flattened_particles

                for attr_el in complex_type.findall(".//xsd:attribute", NS_MAP):
                    attr_info = self._parse_attribute(attr_el)
                    attributes.append(attr_info)

            # Determine element semantic role from schema structure
            has_child_elements = any(
                p["kind"] in ("element_ref", "group_ref", "local_element")
                for p in particles
            )
            if has_child_elements and not is_mixed:
                category = "entity"
            elif is_mixed and has_child_elements:
                category = "mixed_property"
            else:
                category = "scalar_property"

            self.schema_info["elements"][ename] = {
                "name": ename,
                "type": etype or ("complexType" if complex_type is not None else "any"),
                "category": category,
                "is_complex": complex_type is not None,
                "is_mixed": is_mixed,
                "attributes": attributes,
                "particles": particles,
            }

            node_id = f"xsd_el:{ename}"
            self.schema_graph.add_node(
                node_id,
                id=node_id,
                label=ename,
                tag="xsd:element",
                node_type=f"xsd_{category}",
                properties={
                    "name": ename,
                    "type": etype or "complexType",
                    "category": category,
                    "mixed": str(is_mixed),
                    "attributes": ", ".join(
                        f"@{a['name']} ({a['use']})" for a in attributes
                    )
                    or "none",
                },
            )

            # Add attribute nodes in schema graph
            for attr in attributes:
                attr_node_id = f"xsd_attr:{ename}.@{attr['name']}"
                self.schema_graph.add_node(
                    attr_node_id,
                    id=attr_node_id,
                    label=f"@{attr['name']}",
                    tag="xsd:attribute",
                    node_type="xsd_attribute",
                    properties=attr,
                )
                self.schema_graph.add_edge(
                    node_id,
                    attr_node_id,
                    id=f"e_{node_id}_{attr_node_id}",
                    label=attr["use"],
                    edge_type="HAS_ATTRIBUTE",
                )

        # 3. Connect schema graph edges for particles (element refs and group refs)
        for ename, einfo in self.schema_info["elements"].items():
            src_id = f"xsd_el:{ename}"
            for p in einfo["particles"]:
                card = f"{p['minOccurs']}..{'*' if p['maxOccurs'] == 'unbounded' else p['maxOccurs']}"
                edge_label = f"{card} ({p['compositor']})" if p.get("compositor") == "choice" else card
                if p["kind"] in ("element_ref", "local_element"):
                    target_id = f"xsd_el:{p['target']}"
                    if self.schema_graph.has_node(target_id):
                        self.schema_graph.add_edge(
                            src_id,
                            target_id,
                            id=f"e_{src_id}_{target_id}",
                            label=edge_label,
                            edge_type="CONTAINS_ELEMENT",
                            cardinality=card,
                            compositor=p.get("compositor", "sequence"),
                        )
                elif p["kind"] == "group_ref":
                    target_id = f"xsd_group:{p['target']}"
                    if self.schema_graph.has_node(target_id):
                        self.schema_graph.add_edge(
                            src_id,
                            target_id,
                            id=f"e_{src_id}_{target_id}",
                            label=edge_label,
                            edge_type="USES_GROUP",
                            cardinality=card,
                        )

        for gname, ginfo in self.schema_info["groups"].items():
            src_id = f"xsd_group:{gname}"
            for p in ginfo["children"]:
                card = f"{p['minOccurs']}..{'*' if p['maxOccurs'] == 'unbounded' else p['maxOccurs']}"
                edge_label = f"{card} ({p['compositor']})" if p.get("compositor") == "choice" else card
                if p["kind"] in ("element_ref", "local_element"):
                    target_id = f"xsd_el:{p['target']}"
                    if self.schema_graph.has_node(target_id):
                        self.schema_graph.add_edge(
                            src_id,
                            target_id,
                            id=f"e_{src_id}_{target_id}",
                            label=edge_label,
                            edge_type="GROUP_CONTAINS",
                            cardinality=card,
                            compositor=p.get("compositor", "sequence"),
                        )

        # Resolve expanded allowed children for every element (flattening group_refs)
        for ename, einfo in self.schema_info["elements"].items():
            resolved = []
            for p in einfo["particles"]:
                if p["kind"] == "group_ref" and p["target"] in self.schema_info["groups"]:
                    for gp in self.schema_info["groups"][p["target"]]["children"]:
                        resolved.append({**gp, "via_group": p["target"]})
                else:
                    resolved.append(p)
            einfo["resolved_children"] = resolved

    def _extract_particles(
        self, container: etree._Element, current_compositor: str = "sequence"
    ) -> List[Dict[str, Any]]:
        particles: List[Dict[str, Any]] = []
        for child in container:
            lname = local_name(child.tag)
            if lname in ("sequence", "choice", "all"):
                c_min = child.get("minOccurs", "1")
                c_max = child.get("maxOccurs", "1")
                sub = self._extract_particles(child, current_compositor=lname)
                for item in sub:
                    if c_min != "1" or c_max != "1":
                        item["minOccurs"] = item.get("minOccurs", c_min)
                        item["maxOccurs"] = item.get("maxOccurs", c_max)
                    particles.append(item)
            elif lname == "element":
                ref = child.get("ref")
                name = child.get("name")
                particles.append(
                    {
                        "kind": "element_ref" if ref else "local_element",
                        "target": ref or name or "unknown",
                        "minOccurs": child.get("minOccurs", "1"),
                        "maxOccurs": child.get("maxOccurs", "1"),
                        "compositor": current_compositor,
                    }
                )
            elif lname == "group":
                ref = child.get("ref")
                if ref:
                    particles.append(
                        {
                            "kind": "group_ref",
                            "target": ref,
                            "minOccurs": child.get("minOccurs", "1"),
                            "maxOccurs": child.get("maxOccurs", "1"),
                            "compositor": current_compositor,
                        }
                    )
        return particles

    def _parse_attribute(self, attr_el: etree._Element) -> Dict[str, Any]:
        name = attr_el.get("name", "")
        use = attr_el.get("use", "optional")
        base_type = attr_el.get("type", "xsd:string")
        restrictions: Dict[str, Any] = {}
        restr_el = attr_el.find(".//xsd:restriction", NS_MAP)
        if restr_el is not None:
            base_type = restr_el.get("base", base_type)
            enums = [
                e.get("value")
                for e in restr_el.findall("xsd:enumeration", NS_MAP)
                if e.get("value") is not None
            ]
            if enums:
                restrictions["enumeration"] = enums
            min_inc = restr_el.find("xsd:minInclusive", NS_MAP)
            max_inc = restr_el.find("xsd:maxInclusive", NS_MAP)
            if min_inc is not None:
                restrictions["minInclusive"] = min_inc.get("value")
            if max_inc is not None:
                restrictions["maxInclusive"] = max_inc.get("value")
        return {
            "name": name,
            "use": use,
            "base_type": base_type,
            "restrictions": restrictions,
        }

    def to_cytoscape(self) -> Dict[str, Any]:
        nodes = []
        edges = []
        for nid, data in self.schema_graph.nodes(data=True):
            nodes.append({"data": dict(data)})
        for u, v, data in self.schema_graph.edges(data=True):
            edges.append({"data": {"source": u, "target": v, **data}})
        return {
            "mode": "schema",
            "nodes": nodes,
            "edges": edges,
            "schema_info": self.schema_info,
        }


class XMLGraphWorkspace:
    """Loads an XML Document + XSD Schema, validates it, and indexes it into an interactive graph."""

    REFERENCE_TAGS = {"code", "see", "seeAlso", "manif", "seecat", "subcat"}
    INLINE_PROPERTY_TAGS = {"title", "version", "nemod", "warning"}

    def __init__(self, xml_path: Path, xsd_path: Path):
        self.xml_path = xml_path
        self.xsd_path = xsd_path
        t0 = time.perf_counter()

        self.schema_analyzer = XSDSchemaAnalyzer(xsd_path)
        self.xml_doc = etree.parse(str(xml_path))
        self.is_valid = self.schema_analyzer.xml_schema.validate(self.xml_doc)
        self.validation_errors = [
            {
                "line": err.line,
                "column": err.column,
                "message": err.message,
                "level": err.level_name,
            }
            for err in list(self.schema_analyzer.xml_schema.error_log)[:50]
        ]

        # Indexed nodes and adjacency
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.parent_to_children: Dict[str, List[str]] = defaultdict(list)
        self.semantic_edges: List[Dict[str, Any]] = []
        self.node_semantic_edges: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # Lookups for fast search and cross-referencing
        self.root_id: str = "n_0"
        self.main_term_by_title: Dict[str, str] = {}  # lowercase title -> node_id
        self.code_to_node_id: Dict[str, str] = {}  # "Q27.8" -> "code:Q27.8"
        self.code_to_terms: Dict[str, List[str]] = defaultdict(list)
        self.tag_counts: Counter = Counter()

        # Root-to-code-leaf trajectory index for multi-keyword trajectory ranking
        self.trajectories: List[Dict[str, Any]] = []
        self.node_to_trajectory_ids: Dict[str, List[int]] = defaultdict(list)
        self.node_search_text: Dict[str, str] = {}
        self.node_title_lower: Dict[str, str] = {}

        self._build_index()
        self.load_time_ms = round((time.perf_counter() - t0) * 1000, 1)

    def _is_entity_element(self, el: etree._Element) -> bool:
        tag = local_name(el.tag)
        el_schema = self.schema_analyzer.schema_info["elements"].get(tag)
        if el_schema:
            return el_schema["category"] == "entity"
        child_tags = [local_name(c.tag) for c in el if isinstance(c.tag, str)]
        return len(child_tags) > 0 and tag not in self.INLINE_PROPERTY_TAGS

    def _extract_title_and_nemod(self, el: etree._Element) -> Tuple[str, str]:
        title_el = el.find("title")
        if title_el is not None:
            base_text = (title_el.text or "").strip()
            nemods = []
            tail_parts = []
            for child in title_el:
                if local_name(child.tag) == "nemod" and child.text:
                    nemods.append(child.text.strip())
                if child.tail and child.tail.strip():
                    tail_parts.append(child.tail.strip())
            full_title = " ".join([p for p in [base_text] + tail_parts if p]).strip()
            nemod_str = " ".join(nemods).strip()
            return full_title or nemod_str, nemod_str

        for attr_key in ("name", "id", "title", "label", "code", "col"):
            if el.get(attr_key):
                return f"{local_name(el.tag)} [{el.get(attr_key)}]", ""
        if el.text and el.text.strip():
            txt = el.text.strip()
            return (txt[:45] + "…") if len(txt) > 45 else txt, ""
        return local_name(el.tag), ""

    def _build_index(self) -> None:
        root_el = self.xml_doc.getroot()
        counter = 0

        for el in self.xml_doc.iter():
            if isinstance(el.tag, str):
                tname = local_name(el.tag)
                if tname != "letter":
                    self.tag_counts[tname] += 1

        def walk(el: etree._Element, parent_id: Optional[str], depth: int, path_titles: List[str]) -> str:
            nonlocal counter
            node_id = f"n_{counter}"
            counter += 1

            tag = local_name(el.tag)
            title, nemod = self._extract_title_and_nemod(el)
            attrs = {k: v for k, v in el.attrib.items()}
            props: Dict[str, Any] = {}
            ref_items: List[Tuple[str, str]] = []

            entity_children: List[etree._Element] = []
            for child in el:
                if not isinstance(child.tag, str):
                    continue
                ctag = local_name(child.tag)
                if ctag == "letter":
                    # Bypass <letter> container nodes and attach <mainTerm> children directly to root
                    for grand_child in child:
                        if isinstance(grand_child.tag, str) and self._is_entity_element(grand_child):
                            entity_children.append(grand_child)
                elif self._is_entity_element(child):
                    entity_children.append(child)
                elif ctag == "title":
                    props["title"] = title
                    if nemod:
                        props["nemod"] = nemod
                else:
                    val = "".join(child.itertext()).strip()
                    if val:
                        if ctag in props:
                            if isinstance(props[ctag], list):
                                props[ctag].append(val)
                            else:
                                props[ctag] = [props[ctag], val]
                        else:
                            props[ctag] = val
                        if ctag in self.REFERENCE_TAGS:
                            ref_items.append((ctag, val))

            # Build display label
            if tag == "ICD10CM.index":
                display_label = f"{title or 'ICD-10-CM Index'} (v{props.get('version', '')})"
            elif tag == "mainTerm":
                display_label = title
                if title:
                    self.main_term_by_title[title.lower()] = node_id
            elif tag == "term":
                lvl = attrs.get("level", "")
                display_label = f"{title}" if title else f"term (L{lvl})"
            else:
                display_label = title or tag

            current_path = path_titles + ([title] if title else [tag])

            node_data: Dict[str, Any] = {
                "id": node_id,
                "tag": tag,
                "node_type": tag if tag in ("ICD10CM.index", "mainTerm", "term") else "entity",
                "label": display_label,
                "title": title,
                "nemod": nemod,
                "level": int(attrs["level"]) if "level" in attrs and attrs["level"].isdigit() else depth,
                "depth": depth,
                "parent_id": parent_id,
                "attributes": attrs,
                "properties": props,
                "line": el.sourceline or 1,
                "breadcrumb": " › ".join(current_path),
                "child_count": len(entity_children),
                "has_children": len(entity_children) > 0 or len(ref_items) > 0,
            }
            self.nodes[node_id] = node_data
            if parent_id is not None:
                self.parent_to_children[parent_id].append(node_id)

            # Register semantic reference nodes & edges (code, see, seeAlso, manif, seecat, subcat)
            for ref_tag, ref_val in ref_items:
                if ref_tag in ("code", "manif", "seecat", "subcat"):
                    code_node_id = f"{ref_tag}:{ref_val}"
                    if code_node_id not in self.nodes:
                        self.nodes[code_node_id] = {
                            "id": code_node_id,
                            "tag": ref_tag,
                            "node_type": ref_tag,
                            "label": f"{ref_tag.upper()}: {ref_val}" if ref_tag != "code" else ref_val,
                            "title": ref_val,
                            "nemod": "",
                            "level": depth + 1,
                            "depth": depth + 1,
                            "parent_id": None,
                            "attributes": {},
                            "properties": {ref_tag: ref_val},
                            "line": el.sourceline or 1,
                            "breadcrumb": f"ICD-10 {ref_tag.upper()} › {ref_val}",
                            "child_count": 0,
                            "has_children": True,
                        }
                    self.code_to_node_id[ref_val.upper()] = code_node_id
                    self.code_to_terms[code_node_id].append(node_id)
                    edge = {
                        "id": f"e_{node_id}_{code_node_id}",
                        "source": node_id,
                        "target": code_node_id,
                        "label": "CODES_TO" if ref_tag == "code" else ref_tag.upper(),
                        "edge_type": "CODES_TO" if ref_tag == "code" else ref_tag.upper(),
                    }
                    self.node_semantic_edges[node_id].append(edge)
                    self.node_semantic_edges[code_node_id].append(edge)
                elif ref_tag in ("see", "seeAlso"):
                    self.semantic_edges.append(
                        {
                            "source": node_id,
                            "ref_tag": ref_tag,
                            "ref_text": ref_val,
                            "line": el.sourceline or 1,
                        }
                    )

            for child_el in entity_children:
                walk(child_el, node_id, depth + 1, current_path)

            return node_id

        self.root_id = walk(root_el, None, 0, [])

        # Resolve <see> and <seeAlso> references to target mainTerms (or virtual reference nodes)
        for item in self.semantic_edges:
            src_id = item["source"]
            ref_tag = item["ref_tag"]
            ref_text = item["ref_text"]
            edge_type = "SEE" if ref_tag == "see" else "SEE_ALSO"

            head_candidate = ref_text.split(",")[0].strip().lower()
            target_id = self.main_term_by_title.get(ref_text.lower()) or self.main_term_by_title.get(head_candidate)

            if not target_id:
                target_id = f"ref:{ref_tag}:{ref_text}"
                if target_id not in self.nodes:
                    self.nodes[target_id] = {
                        "id": target_id,
                        "tag": ref_tag,
                        "node_type": ref_tag,
                        "label": f"{'See' if ref_tag == 'see' else 'See Also'}: {ref_text}",
                        "title": ref_text,
                        "nemod": "",
                        "level": 2,
                        "depth": 2,
                        "parent_id": None,
                        "attributes": {},
                        "properties": {ref_tag: ref_text},
                        "line": item["line"],
                        "breadcrumb": f"Reference › {ref_text}",
                        "child_count": 0,
                        "has_children": False,
                    }

            edge = {
                "id": f"e_{src_id}_{target_id}_{edge_type}",
                "source": src_id,
                "target": target_id,
                "label": edge_type,
                "edge_type": edge_type,
                "ref_text": ref_text,
            }
            self.node_semantic_edges[src_id].append(edge)
            self.node_semantic_edges[target_id].append(edge)

        for code_node_id, term_ids in self.code_to_terms.items():
            if code_node_id in self.nodes:
                self.nodes[code_node_id]["child_count"] = len(term_ids)
                self.nodes[code_node_id]["properties"]["linked_terms_count"] = len(term_ids)

        # Precompute searchable text for every node (excluding root n_0 and parenthetical <nemod> modifiers)
        for nid, ndata in self.nodes.items():
            if nid == self.root_id:
                continue
            title_val = (ndata.get("title") or "").strip().lower()
            self.node_title_lower[nid] = title_val
            self.node_search_text[nid] = title_val

        # Precompute all root-to-code-leaf trajectories in document order
        for nid, ndata in self.nodes.items():
            if ndata["tag"] not in ("mainTerm", "term", "entity"):
                continue
            for sedge in self.node_semantic_edges.get(nid, []):
                if sedge["source"] == nid and sedge["edge_type"] in ("CODES_TO", "MANIF", "SEECAT", "SUBCAT"):
                    code_node_id = sedge["target"]
                    chain: List[str] = []
                    curr: Optional[str] = nid
                    while curr is not None:
                        chain.append(curr)
                        curr = self.nodes.get(curr, {}).get("parent_id")
                    chain.reverse()
                    traj_nodes = tuple(chain + [code_node_id])
                    hier_edges = [f"e_hier_{chain[i]}_{chain[i + 1]}" for i in range(len(chain) - 1)]
                    traj_edges = tuple(hier_edges + [sedge["id"]])
                    t_idx = len(self.trajectories)
                    code_node = self.nodes.get(code_node_id, {})
                    code_label = code_node.get("label", code_node_id)
                    self.trajectories.append(
                        {
                            "id": t_idx,
                            "nodes": traj_nodes,
                            "edges": traj_edges,
                            "term_id": nid,
                            "code_id": code_node_id,
                            "code_edge_id": sedge["id"],
                            "code": code_node.get("title", ""),
                            "breadcrumb": f"{ndata.get('breadcrumb', '')} → {code_label}",
                        }
                    )
                    for path_nid in traj_nodes[1:]:
                        self.node_to_trajectory_ids[path_nid].append(t_idx)

    def get_summary(self) -> Dict[str, Any]:
        return {
            "xml_filename": self.xml_path.name,
            "xml_size_bytes": self.xml_path.stat().st_size,
            "xsd_filename": self.xsd_path.name,
            "is_valid": self.is_valid,
            "validation_errors": self.validation_errors,
            "load_time_ms": self.load_time_ms,
            "total_graph_nodes": len(self.nodes),
            "total_trajectories": len(self.trajectories),
            "tag_counts": dict(self.tag_counts),
            "schema_info": self.schema_analyzer.schema_info,
        }

    def _add_ancestor_hierarchy(self, node_ids: Set[str]) -> Set[str]:
        """Returns a new set containing node_ids plus all their hierarchical ancestors up to root."""
        with_ancestors = set(node_ids)
        for nid in list(node_ids):
            curr = self.nodes.get(nid, {}).get("parent_id")
            while curr:
                with_ancestors.add(curr)
                curr = self.nodes.get(curr, {}).get("parent_id")
        return with_ancestors

    def _build_subgraph_payload(
        self,
        included_node_ids: Set[str],
        include_semantic: bool = True,
        highlight_ids: Optional[Set[str]] = None,
        allowed_semantic_edge_ids: Optional[Set[str]] = None,
        node_match_details: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        highlight_ids = highlight_ids or set()
        node_match_details = node_match_details or {}
        cy_nodes = []
        cy_edges = []
        seen_edges: Set[str] = set()

        if include_semantic and allowed_semantic_edge_ids is None:
            extra_semantic_nodes: Set[str] = set()
            for nid in list(included_node_ids):
                for edge in self.node_semantic_edges.get(nid, []):
                    if edge["source"] == nid:
                        extra_semantic_nodes.add(edge["target"])
            if len(included_node_ids) + len(extra_semantic_nodes) <= 350:
                included_node_ids |= extra_semantic_nodes

        for nid in included_node_ids:
            node = self.nodes.get(nid)
            if not node:
                continue
            data = dict(node)
            data["highlighted"] = nid in highlight_ids
            if nid in node_match_details:
                data["keyword_occurrences"] = node_match_details[nid]["occurrences"]
                data["matched_keywords"] = node_match_details[nid]["matched_keywords"]
            children = self.parent_to_children.get(nid, [])
            visible_children = sum(1 for c in children if c in included_node_ids)
            data["expanded"] = len(children) > 0 and visible_children > 0
            cy_nodes.append({"data": data})

            pid = node.get("parent_id")
            if pid and pid in included_node_ids:
                eid = f"e_hier_{pid}_{nid}"
                if eid not in seen_edges:
                    seen_edges.add(eid)
                    edge_label = f"HAS_{node['tag'].upper()}"
                    if node["tag"] == "term":
                        edge_label = f"SUBTERM (L{node.get('level', 1)})"
                    cy_edges.append(
                        {
                            "data": {
                                "id": eid,
                                "source": pid,
                                "target": nid,
                                "label": edge_label,
                                "edge_type": "HIERARCHY",
                            }
                        }
                    )

            for sedge in self.node_semantic_edges.get(nid, []):
                if sedge["source"] in included_node_ids and sedge["target"] in included_node_ids:
                    if allowed_semantic_edge_ids is not None and sedge["id"] not in allowed_semantic_edge_ids:
                        continue
                    if sedge["id"] not in seen_edges:
                        seen_edges.add(sedge["id"])
                        cy_edges.append({"data": dict(sedge)})

        return {
            "mode": "document",
            "nodes": cy_nodes,
            "edges": cy_edges,
            "node_count": len(cy_nodes),
            "edge_count": len(cy_edges),
        }

    def get_initial_subgraph(
        self, max_main_terms: int = 12, max_depth: int = 2
    ) -> Dict[str, Any]:
        included: Set[str] = {self.root_id}
        main_terms = self.parent_to_children.get(self.root_id, [])[:max_main_terms]
        for mt_id in main_terms:
            included.add(mt_id)
            if max_depth >= 1:
                for t1_id in self.parent_to_children.get(mt_id, [])[:6]:
                    included.add(t1_id)
                    if max_depth >= 2:
                        for t2_id in self.parent_to_children.get(t1_id, [])[:4]:
                            included.add(t2_id)

        return self._build_subgraph_payload(
            included, include_semantic=True, highlight_ids={self.root_id}
        )

    def expand_node(self, node_id: str, limit: int = 25) -> Dict[str, Any]:
        if node_id not in self.nodes:
            return {"nodes": [], "edges": [], "node_count": 0, "edge_count": 0}

        core_nodes: Set[str] = {node_id}
        allowed_edges: Set[str] = set()

        # Direct entity children of node_id
        for child_id in self.parent_to_children.get(node_id, [])[:limit]:
            core_nodes.add(child_id)
            for sedge in self.node_semantic_edges.get(child_id, []):
                if sedge["source"] == child_id:
                    core_nodes.add(sedge["target"])
                    allowed_edges.add(sedge["id"])

        # Direct semantic edges of node_id (including terms linking to a code node)
        for sedge in self.node_semantic_edges.get(node_id, [])[:limit]:
            core_nodes.add(sedge["source"])
            core_nodes.add(sedge["target"])
            allowed_edges.add(sedge["id"])

        included = self._add_ancestor_hierarchy(core_nodes)

        return self._build_subgraph_payload(
            included,
            include_semantic=False,
            highlight_ids={node_id},
            allowed_semantic_edge_ids=allowed_edges,
        )

    def search_subgraph(
        self, query: str, max_results: int = 15, include_children: bool = True
    ) -> Dict[str, Any]:
        """
        Trajectory-ranking search engine:
        1. Splits `query` into semicolon-separated keywords.
        2. Extracts ALL trajectories (connected paths from root `n_0` to final `code` leaves)
           containing at least one of the keywords.
        3. Ranks all matching trajectories by the number of keyword occurrences found along the trajectory.
        4. Returns the subgraph corresponding to the top N (`max_results`) ranked trajectories.
        """
        raw_keywords = [k.strip() for k in query.split(";") if k.strip()]
        if not raw_keywords:
            return self.get_initial_subgraph()

        keywords: List[Tuple[str, str, Optional[re.Pattern]]] = []
        seen_kw: Set[str] = set()
        for rk in raw_keywords:
            kl = rk.lower()
            if kl not in seen_kw:
                seen_kw.add(kl)
                prefix = r"\b" if kl[0].isalnum() else ""
                suffix = r"\b" if kl[-1].isalnum() else ""
                pat = re.compile(rf"{prefix}{re.escape(kl)}{suffix}") if (prefix or suffix) else None
                keywords.append((kl, rk, pat))

        # Check which keywords have whole-word matches in the graph; fall back to substring only if 0 whole-word matches
        use_word_boundary: List[bool] = []
        for kl, _, pat in keywords:
            if pat is None:
                use_word_boundary.append(False)
            else:
                has_wb = any(pat.search(stext) for stext in self.node_search_text.values() if kl in stext)
                use_word_boundary.append(has_wb)

        node_match_details: Dict[str, Dict[str, Any]] = {}
        traj_occurrences: Counter = Counter()
        traj_kw_mask: Dict[int, int] = defaultdict(int)
        traj_exact_hits: Counter = Counter()

        # Scan all indexed nodes and accumulate keyword occurrence counts onto root-to-code trajectories
        for nid, stext in self.node_search_text.items():
            if not stext:
                continue
            t_ids = self.node_to_trajectory_ids.get(nid)
            if not t_ids:
                continue
            occ_count = 0
            kw_mask = 0
            exact_count = 0
            matched_kws: List[str] = []
            for kw_idx, (kl, kdisp, pat) in enumerate(keywords):
                if kl not in stext:
                    continue
                if use_word_boundary[kw_idx] and pat is not None:
                    matched = bool(pat.search(stext))
                else:
                    matched = True
                if matched:
                    occ_count += 1
                    kw_mask |= 1 << kw_idx
                    matched_kws.append(kdisp)
                    if stext == kl:
                        exact_count += 1
            if occ_count > 0:
                node_match_details[nid] = {
                    "occurrences": occ_count,
                    "matched_keywords": matched_kws,
                }
                for tid in t_ids:
                    traj_occurrences[tid] += occ_count
                    traj_kw_mask[tid] |= kw_mask
                    if exact_count > 0:
                        traj_exact_hits[tid] += exact_count

        all_matching_tids = set(traj_kw_mask.keys())
        if not all_matching_tids:
            return {
                "mode": "document",
                "nodes": [],
                "edges": [],
                "node_count": 0,
                "edge_count": 0,
                "matched_count": 0,
                "total_matching_trajectories": 0,
                "top_n": max_results,
                "keywords": [kdisp for _, kdisp, _ in keywords],
                "trajectories": [],
                "query": query,
            }

        # Rank trajectories by:
        # 1. Total keyword occurrences along the trajectory (descending)
        # 2. Number of distinct user keywords matched along the trajectory (descending)
        # 3. Exact node title/code matches along the trajectory (descending)
        # 4. Shorter trajectory path (ascending depth)
        # 5. Document order tie-breaker
        ranked_tids = sorted(
            all_matching_tids,
            key=lambda tid: (
                traj_occurrences[tid],
                traj_kw_mask[tid].bit_count(),
                traj_exact_hits.get(tid, 0),
                -len(self.trajectories[tid]["nodes"]),
                -tid,
            ),
            reverse=True,
        )

        top_tids = ranked_tids[:max_results]
        included_nodes: Set[str] = set()
        allowed_semantic_edges: Set[str] = set()
        highlighted_nodes: Set[str] = set()
        ranked_trajectories_payload: List[Dict[str, Any]] = []

        for rank_idx, tid in enumerate(top_tids, start=1):
            traj = self.trajectories[tid]
            for nid in traj["nodes"]:
                included_nodes.add(nid)
                if nid in node_match_details:
                    highlighted_nodes.add(nid)
            allowed_semantic_edges.add(traj["code_edge_id"])

            kw_mask = traj_kw_mask[tid]
            traj_kws = [kdisp for idx, (_, kdisp, _) in enumerate(keywords) if (kw_mask & (1 << idx))]

            ranked_trajectories_payload.append(
                {
                    "rank": rank_idx,
                    "trajectory_id": tid,
                    "keyword_occurrences": traj_occurrences[tid],
                    "distinct_keywords_matched": kw_mask.bit_count(),
                    "matched_keywords": traj_kws,
                    "code": traj["code"],
                    "code_id": traj["code_id"],
                    "term_id": traj["term_id"],
                    "breadcrumb": traj["breadcrumb"],
                    "nodes": list(traj["nodes"]),
                    "edges": list(traj["edges"]),
                }
            )

        res = self._build_subgraph_payload(
            included_nodes,
            include_semantic=False,
            highlight_ids=highlighted_nodes,
            allowed_semantic_edge_ids=allowed_semantic_edges,
            node_match_details=node_match_details,
        )
        res["matched_count"] = len(ranked_trajectories_payload)
        res["total_matching_trajectories"] = len(ranked_tids)
        res["top_n"] = max_results
        res["keywords"] = [kdisp for _, kdisp, _ in keywords]
        res["trajectories"] = ranked_trajectories_payload
        res["matched_ids"] = list(highlighted_nodes)
        res["query"] = query
        return res

    def get_node_detail(self, node_id: str) -> Dict[str, Any]:
        node = self.nodes.get(node_id)
        if not node:
            # Check if it's a schema graph node
            if self.schema_analyzer.schema_graph.has_node(node_id):
                sdata = dict(self.schema_analyzer.schema_graph.nodes[node_id])
                tag_name = sdata.get("properties", {}).get("name", "")
                return {
                    "node": sdata,
                    "xsd_definition": self.schema_analyzer.schema_info["elements"].get(tag_name, {}),
                    "children_preview": [],
                    "semantic_links": [],
                }
            return {"error": f"Node {node_id} not found"}

        tag = node.get("tag", "")
        xsd_def = self.schema_analyzer.schema_info["elements"].get(tag, {})
        children_preview = [
            {
                "id": cid,
                "label": self.nodes[cid]["label"],
                "tag": self.nodes[cid]["tag"],
                "code": self.nodes[cid].get("properties", {}).get("code", ""),
            }
            for cid in self.parent_to_children.get(node_id, [])[:200]
            if cid in self.nodes
        ]
        if node_id in self.code_to_terms:
            children_preview = [
                {
                    "id": tid,
                    "label": self.nodes[tid]["breadcrumb"],
                    "tag": self.nodes[tid]["tag"],
                    "code": node.get("title", ""),
                }
                for tid in self.code_to_terms[node_id][:200]
                if tid in self.nodes
            ]

        return {
            "node": node,
            "xsd_definition": xsd_def,
            "children_preview": children_preview,
            "total_children": len(self.parent_to_children.get(node_id, []))
            or len(self.code_to_terms.get(node_id, [])),
            "semantic_links": self.node_semantic_edges.get(node_id, [])[:30],
        }

    TAG_TO_GQL_LABEL: Dict[str, str] = {
        "ICD10CM.index": "IndexRoot",
        "mainTerm": "MainTerm",
        "term": "SubTerm",
        "code": "ICD10Code",
        "see": "SeeReference",
        "seeAlso": "SeeAlsoReference",
        "manif": "ManifestationCode",
        "seecat": "SeeCategory",
        "subcat": "SubCategory",
        "xsd:element": "XsdElement",
        "xsd:group": "XsdGroup",
        "xsd:attribute": "XsdAttribute",
    }

    @staticmethod
    def _generate_database_ddls() -> Dict[str, Any]:
        bigquery_ddl = """-- ============================================================================
-- Google Cloud BigQuery Graph Ingestion DDL (ISO GQL Property Graph)
-- ============================================================================

-- 1. Create Node Table (load from `nodes` array or `.jsonl` where record_type = 'node')
CREATE TABLE IF NOT EXISTS `project_id.dataset_id.xml_graph_nodes` (
  node_id STRING NOT NULL OPTIONS(description="Unique primary key of the graph node"),
  node_label STRING NOT NULL OPTIONS(description="GQL node label (IndexRoot, MainTerm, SubTerm, ICD10Code, SeeReference)"),
  xml_tag STRING OPTIONS(description="Source XML or XSD element tag name"),
  title STRING OPTIONS(description="Clinical term title, code value, or element name"),
  display_label STRING OPTIONS(description="Human-readable display label"),
  nemod STRING OPTIONS(description="Non-essential modifier text enclosed in parentheses"),
  code STRING OPTIONS(description="Associated ICD-10-CM diagnosis code if present"),
  level INT64 OPTIONS(description="Hierarchical indentation level (0=root, 1..9=subterm depth)"),
  depth INT64 OPTIONS(description="Tree depth from XML document root"),
  parent_id STRING OPTIONS(description="Parent node_id in the XML hierarchy"),
  breadcrumb STRING OPTIONS(description="Full hierarchical path from root to this node"),
  source_line INT64 OPTIONS(description="Line number in the original XML document"),
  properties JSON OPTIONS(description="Additional XML element properties and attributes"),
  PRIMARY KEY (node_id) NOT ENFORCED
);

-- 2. Create Edge Table (load from `edges` array or `.jsonl` where record_type = 'edge')
CREATE TABLE IF NOT EXISTS `project_id.dataset_id.xml_graph_edges` (
  edge_id STRING NOT NULL OPTIONS(description="Unique primary key of the directed edge"),
  source_id STRING NOT NULL OPTIONS(description="Source node_id referencing xml_graph_nodes.node_id"),
  destination_id STRING NOT NULL OPTIONS(description="Destination node_id referencing xml_graph_nodes.node_id"),
  edge_label STRING NOT NULL OPTIONS(description="Human-readable edge label (e.g., HAS_MAINTERM, SUBTERM, CODES_TO, SEE)"),
  edge_type STRING NOT NULL OPTIONS(description="Semantic edge category (HIERARCHY, CODES_TO, SEE, SEE_ALSO, MANIF)"),
  ref_text STRING OPTIONS(description="Cross-reference target text for SEE / SEE_ALSO edges"),
  properties JSON OPTIONS(description="Additional edge metadata"),
  PRIMARY KEY (edge_id) NOT ENFORCED,
  FOREIGN KEY (source_id) REFERENCES `project_id.dataset_id.xml_graph_nodes`(node_id) NOT ENFORCED,
  FOREIGN KEY (destination_id) REFERENCES `project_id.dataset_id.xml_graph_nodes`(node_id) NOT ENFORCED
);

-- 3. Create BigQuery Property Graph (follows BigQuery Graph best practices: safe aliases, PK/FK keys, scoped properties)
CREATE OR REPLACE PROPERTY GRAPH `project_id.dataset_id.xml_knowledge_graph`
  NODE TABLES (
    `project_id.dataset_id.xml_graph_nodes` AS GraphNode
      KEY (node_id)
      LABEL GraphNode
      PROPERTIES (
        node_id,
        node_label,
        xml_tag,
        title,
        display_label,
        nemod,
        code,
        level,
        depth,
        parent_id,
        breadcrumb,
        source_line
      )
  )
  EDGE TABLES (
    `project_id.dataset_id.xml_graph_edges` AS GraphEdge
      KEY (edge_id)
      SOURCE KEY (source_id) REFERENCES GraphNode (node_id)
      DESTINATION KEY (destination_id) REFERENCES GraphNode (node_id)
      LABEL GraphEdge
      PROPERTIES (
        edge_id,
        edge_label,
        edge_type,
        ref_text
      )
  );"""

        spanner_ddl = """-- ============================================================================
-- Google Cloud Spanner Graph Ingestion DDL (GoogleSQL ISO GQL Property Graph)
-- ============================================================================

-- 1. Create Node Table
CREATE TABLE XmlGraphNode (
  node_id STRING(MAX) NOT NULL,
  node_label STRING(MAX) NOT NULL,
  xml_tag STRING(MAX),
  title STRING(MAX),
  display_label STRING(MAX),
  nemod STRING(MAX),
  code STRING(MAX),
  level INT64,
  depth INT64,
  parent_id STRING(MAX),
  breadcrumb STRING(MAX),
  source_line INT64,
  properties JSON
) PRIMARY KEY (node_id);

-- 2. Create Edge Table
CREATE TABLE XmlGraphEdge (
  edge_id STRING(MAX) NOT NULL,
  source_id STRING(MAX) NOT NULL,
  destination_id STRING(MAX) NOT NULL,
  edge_label STRING(MAX) NOT NULL,
  edge_type STRING(MAX) NOT NULL,
  ref_text STRING(MAX),
  properties JSON,
  CONSTRAINT FK_XmlGraphEdge_Source FOREIGN KEY (source_id) REFERENCES XmlGraphNode (node_id),
  CONSTRAINT FK_XmlGraphEdge_Dest FOREIGN KEY (destination_id) REFERENCES XmlGraphNode (node_id)
) PRIMARY KEY (edge_id);

-- 3. Create Spanner Property Graph
CREATE OR REPLACE PROPERTY GRAPH XmlKnowledgeGraph
  NODE TABLES (
    XmlGraphNode AS GraphNode
      KEY (node_id)
      LABEL GraphNode
      PROPERTIES (
        node_id,
        node_label,
        xml_tag,
        title,
        display_label,
        nemod,
        code,
        level,
        depth,
        parent_id,
        breadcrumb,
        source_line
      )
  )
  EDGE TABLES (
    XmlGraphEdge AS GraphEdge
      KEY (edge_id)
      SOURCE KEY (source_id) REFERENCES GraphNode (node_id)
      DESTINATION KEY (destination_id) REFERENCES GraphNode (node_id)
      LABEL GraphEdge
      PROPERTIES (
        edge_id,
        edge_label,
        edge_type,
        ref_text
      )
  );"""

        sample_gql_queries = [
            {
                "description": "Find all clinical term trajectories mapping to a specific ICD-10 diagnosis code",
                "gql": (
                    "GRAPH `project_id.dataset_id.xml_knowledge_graph`\n"
                    "MATCH (term:GraphNode)-[e:GraphEdge {edge_type: 'CODES_TO'}]->(code:GraphNode {code: 'Q27.8'})\n"
                    "RETURN term.node_id, term.breadcrumb, term.nemod, code.code"
                ),
            },
            {
                "description": "Traverse hierarchical subterms from a MainTerm down 1 to 5 hops to an ICD-10 code",
                "gql": (
                    "GRAPH `project_id.dataset_id.xml_knowledge_graph`\n"
                    "MATCH (main:GraphNode {node_label: 'MainTerm'})-[:GraphEdge {edge_type: 'HIERARCHY'}]->{1,5}"
                    "(leaf:GraphNode)-[:GraphEdge {edge_type: 'CODES_TO'}]->(c:GraphNode)\n"
                    "RETURN main.title AS main_term, leaf.breadcrumb AS full_clinical_path, c.title AS icd10_code\n"
                    "LIMIT 50"
                ),
            },
        ]

        return {
            "graph_model": "ISO/IEC 39075 GQL Labeled Property Graph (LPG)",
            "bigquery_graph_ddl": bigquery_ddl,
            "spanner_graph_ddl": spanner_ddl,
            "sample_gql_queries": sample_gql_queries,
        }

    def build_property_graph(
        self,
        scope: str = "canvas",
        node_ids: Optional[List[str]] = None,
        edge_ids: Optional[List[str]] = None,
        search_query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Constructs a formal Property Graph object (NetworkX MultiDiGraph + tabular PG-JSON nodes/edges)
        designed for LLM comprehension and direct ingestion into BigQuery Graph and Spanner Graph.
        """
        G = nx.MultiDiGraph(
            name="xml_knowledge_graph",
            xml_source=self.xml_path.name,
            xsd_schema=self.xsd_path.name,
            scope=scope,
        )

        nodes_list: List[Dict[str, Any]] = []
        edges_list: List[Dict[str, Any]] = []
        label_counts: Counter = Counter()
        edge_type_counts: Counter = Counter()

        if scope == "schema":
            for nid, sdata in self.schema_analyzer.schema_graph.nodes(data=True):
                tag = sdata.get("tag", "xsd:element")
                node_label = self.TAG_TO_GQL_LABEL.get(tag, "XsdSchemaNode")
                props = dict(sdata.get("properties", {}))
                record = {
                    "id": nid,
                    "node_id": nid,
                    "label": node_label,
                    "node_label": node_label,
                    "xml_tag": tag,
                    "title": sdata.get("label", nid),
                    "display_label": sdata.get("label", nid),
                    "nemod": None,
                    "code": None,
                    "level": 0,
                    "depth": 0,
                    "parent_id": None,
                    "breadcrumb": f"XSD Schema › {sdata.get('label', nid)}",
                    "source_line": None,
                    "properties": props,
                }
                G.add_node(nid, **record)
                nodes_list.append(record)
                label_counts[node_label] += 1

            for u, v, edata in self.schema_analyzer.schema_graph.edges(data=True):
                eid = edata.get("id", f"e_{u}_{v}")
                etype = edata.get("edge_type", "SCHEMA_RELATION")
                elabel = edata.get("label", etype)
                eprops = {
                    k: val
                    for k, val in edata.items()
                    if k not in ("id", "source", "target", "label", "edge_type")
                }
                erecord = {
                    "id": eid,
                    "edge_id": eid,
                    "source": u,
                    "target": v,
                    "source_id": u,
                    "destination_id": v,
                    "label": elabel,
                    "edge_label": elabel,
                    "edge_type": etype,
                    "ref_text": None,
                    "properties": eprops,
                }
                G.add_edge(u, v, key=eid, **erecord)
                edges_list.append(erecord)
                edge_type_counts[etype] += 1
        else:
            if scope == "canvas" and node_ids:
                included_nids: Set[str] = {nid for nid in node_ids if nid in self.nodes}
            else:
                included_nids = set(self.nodes.keys())
                scope = "full"

            allowed_eids: Optional[Set[str]] = set(edge_ids) if (scope == "canvas" and edge_ids) else None
            seen_edges: Set[str] = set()

            for nid in included_nids:
                ndata = self.nodes.get(nid)
                if not ndata:
                    continue
                tag = ndata.get("tag", "entity")
                node_label = self.TAG_TO_GQL_LABEL.get(tag, "XmlEntity")
                props = dict(ndata.get("properties", {}))
                attrs = dict(ndata.get("attributes", {}))
                merged_props = {**props, **{f"@{k}": v for k, v in attrs.items()}}
                code_val = props.get("code") or (ndata.get("title") if tag == "code" else None)
                if isinstance(code_val, list):
                    code_val = code_val[0]

                record = {
                    "id": nid,
                    "node_id": nid,
                    "label": node_label,
                    "node_label": node_label,
                    "xml_tag": tag,
                    "title": ndata.get("title", ""),
                    "display_label": ndata.get("label", ""),
                    "nemod": ndata.get("nemod") or None,
                    "code": code_val,
                    "level": int(ndata.get("level", 0)),
                    "depth": int(ndata.get("depth", 0)),
                    "parent_id": ndata.get("parent_id") if ndata.get("parent_id") in included_nids else None,
                    "breadcrumb": ndata.get("breadcrumb", ""),
                    "source_line": int(ndata["line"]) if ndata.get("line") else None,
                    "properties": merged_props,
                }
                G.add_node(nid, **record)
                nodes_list.append(record)
                label_counts[node_label] += 1

            for nid in included_nids:
                ndata = self.nodes.get(nid)
                if not ndata:
                    continue
                pid = ndata.get("parent_id")
                if pid and pid in included_nids:
                    eid = f"e_hier_{pid}_{nid}"
                    if (allowed_eids is None or eid in allowed_eids) and eid not in seen_edges:
                        seen_edges.add(eid)
                        elabel = f"HAS_{ndata['tag'].upper()}"
                        if ndata["tag"] == "term":
                            elabel = f"SUBTERM_L{ndata.get('level', 1)}"
                        erecord = {
                            "id": eid,
                            "edge_id": eid,
                            "source": pid,
                            "target": nid,
                            "source_id": pid,
                            "destination_id": nid,
                            "label": elabel,
                            "edge_label": elabel,
                            "edge_type": "HIERARCHY",
                            "ref_text": None,
                            "properties": {"child_level": int(ndata.get("level", 0))},
                        }
                        G.add_edge(pid, nid, key=eid, **erecord)
                        edges_list.append(erecord)
                        edge_type_counts["HIERARCHY"] += 1

                for sedge in self.node_semantic_edges.get(nid, []):
                    u = sedge["source"]
                    v = sedge["target"]
                    eid = sedge["id"]
                    if u in included_nids and v in included_nids:
                        if allowed_eids is not None and eid not in allowed_eids:
                            continue
                        if eid not in seen_edges:
                            seen_edges.add(eid)
                            etype = sedge.get("edge_type", "SEMANTIC_REF")
                            elabel = sedge.get("label", etype)
                            ref_txt = sedge.get("ref_text")
                            erecord = {
                                "id": eid,
                                "edge_id": eid,
                                "source": u,
                                "target": v,
                                "source_id": u,
                                "destination_id": v,
                                "label": elabel,
                                "edge_label": elabel,
                                "edge_type": etype,
                                "ref_text": ref_txt,
                                "properties": {"ref_text": ref_txt} if ref_txt else {},
                            }
                            G.add_edge(u, v, key=eid, **erecord)
                            edges_list.append(erecord)
                            edge_type_counts[etype] += 1

        # Build natural-language trajectories summary for LLM reading
        sample_trajectories: List[str] = []
        for e in edges_list:
            if e["edge_type"] in ("CODES_TO", "SEE", "SEE_ALSO", "MANIF"):
                src_node = G.nodes.get(e["source_id"], {})
                dst_node = G.nodes.get(e["destination_id"], {})
                bc = src_node.get("breadcrumb") or src_node.get("title") or e["source_id"]
                target_title = dst_node.get("title") or e["destination_id"]
                sample_trajectories.append(f"{bc} -[{e['edge_type']}]-> {target_title}")
                if len(sample_trajectories) >= 40:
                    break

        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "graph_name": "xml_knowledge_graph",
            "created_at": created_at,
            "xml_document": self.xml_path.name,
            "xsd_schema": self.xsd_path.name,
            "xsd_valid": self.is_valid,
            "scope": scope,
            "search_query": search_query or None,
            "node_count": G.number_of_nodes(),
            "edge_count": G.number_of_edges(),
            "node_label_counts": dict(label_counts),
            "edge_type_counts": dict(edge_type_counts),
        }

        llm_context = {
            "overview": (
                f"This Labeled Property Graph (LPG) represents the XML document '{self.xml_path.name}' "
                f"validated against the XML Schema '{self.xsd_path.name}'. "
                "It captures both the hierarchical taxonomic tree (IndexRoot -> MainTerm -> SubTerm levels 1..9) "
                "and semantic cross-references (CODES_TO -> ICD10Code hubs, SEE / SEE_ALSO -> cross-referenced terms)."
            ),
            "how_to_read": {
                "nodes": (
                    "Each item in `nodes` has a unique `node_id`, a GQL `node_label` (e.g., MainTerm, SubTerm, ICD10Code), "
                    "a `title`, optional `nemod` (non-essential clinical modifiers), `breadcrumb` (full root-to-node path), "
                    "and `properties`."
                ),
                "edges": (
                    "Each item in `edges` connects `source_id` -> `destination_id` with `edge_type` in "
                    "{HIERARCHY, CODES_TO, SEE, SEE_ALSO, MANIF, SEECAT, SUBCAT}."
                ),
            },
            "clinical_coding_paths_preview": sample_trajectories,
        }

        return {
            "nx_graph": G,
            "format": "PG-JSON (ISO GQL / BigQuery Graph & Spanner Graph Property Graph)",
            "format_version": "1.0",
            "metadata": metadata,
            "llm_context": llm_context,
            "database_ingestion": self._generate_database_ddls(),
            "nodes": nodes_list,
            "edges": edges_list,
        }

    def serialize_property_graph(
        self,
        scope: str = "canvas",
        fmt: str = "pg_json",
        node_ids: Optional[List[str]] = None,
        edge_ids: Optional[List[str]] = None,
        search_query: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Builds the Property Graph object and serializes it into the requested format:
        - 'pg_json': Property Graph JSON (.json) - Self-contained LLM + BigQuery/Spanner Graph JSON
        - 'pg_jsonl': Property Graph JSONL (.jsonl) - Newline-Delimited JSON for direct `bq load` & streaming
        - 'gql_sql': GoogleSQL script (.sql) - Executable DDL + INSERT statements for BigQuery/Spanner Graph
        """
        pg = self.build_property_graph(
            scope=scope,
            node_ids=node_ids,
            edge_ids=edge_ids,
            search_query=search_query,
        )
        meta = pg["metadata"]

        if fmt == "pg_jsonl":
            lines: List[str] = []
            header_record = {
                "record_type": "graph_metadata",
                "format": "PG-JSONL",
                "metadata": meta,
                "llm_context": pg["llm_context"],
                "database_ingestion": pg["database_ingestion"],
            }
            lines.append(json.dumps(header_record, ensure_ascii=False))
            for n in pg["nodes"]:
                lines.append(
                    json.dumps(
                        {
                            "record_type": "node",
                            "node_id": n["node_id"],
                            "node_label": n["node_label"],
                            "xml_tag": n["xml_tag"],
                            "title": n["title"],
                            "display_label": n["display_label"],
                            "nemod": n["nemod"],
                            "code": n["code"],
                            "level": n["level"],
                            "depth": n["depth"],
                            "parent_id": n["parent_id"],
                            "breadcrumb": n["breadcrumb"],
                            "source_line": n["source_line"],
                            "properties": n["properties"],
                        },
                        ensure_ascii=False,
                    )
                )
            for e in pg["edges"]:
                lines.append(
                    json.dumps(
                        {
                            "record_type": "edge",
                            "edge_id": e["edge_id"],
                            "source_id": e["source_id"],
                            "destination_id": e["destination_id"],
                            "edge_label": e["edge_label"],
                            "edge_type": e["edge_type"],
                            "ref_text": e["ref_text"],
                            "properties": e["properties"],
                        },
                        ensure_ascii=False,
                    )
                )
            return "\n".join(lines) + "\n", meta

        if fmt == "gql_sql":
            def sql_str(val: Optional[Any]) -> str:
                if val is None:
                    return "NULL"
                escaped = str(val).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
                return f"'{escaped}'"

            def sql_json(val: Dict[str, Any]) -> str:
                raw = json.dumps(val, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
                return f"JSON '{raw}'"

            sql_parts: List[str] = [
                "-- ============================================================================",
                f"-- Property Graph SQL Export: {meta['xml_document']} ({meta['node_count']} nodes, {meta['edge_count']} edges)",
                f"-- Generated at: {meta['created_at']}",
                "-- Compatible with Google Cloud BigQuery Graph & Cloud Spanner Graph",
                "-- ============================================================================",
                "",
                pg["database_ingestion"]["bigquery_graph_ddl"],
                "",
                "/* --- Cloud Spanner Graph Equivalent DDL ---",
                pg["database_ingestion"]["spanner_graph_ddl"],
                "*/",
                "",
            ]

            max_sql_rows = 5000
            nodes_slice = pg["nodes"][:max_sql_rows]
            edges_slice = pg["edges"][:max_sql_rows]

            if nodes_slice:
                sql_parts.append("-- Insert Graph Nodes")
                batch_size = 200
                for i in range(0, len(nodes_slice), batch_size):
                    batch = nodes_slice[i : i + batch_size]
                    sql_parts.append(
                        "INSERT INTO `project_id.dataset_id.xml_graph_nodes` "
                        "(node_id, node_label, xml_tag, title, display_label, nemod, code, level, depth, parent_id, breadcrumb, source_line, properties) VALUES"
                    )
                    row_strs = []
                    for n in batch:
                        row_strs.append(
                            f"  ({sql_str(n['node_id'])}, {sql_str(n['node_label'])}, {sql_str(n['xml_tag'])}, "
                            f"{sql_str(n['title'])}, {sql_str(n['display_label'])}, {sql_str(n['nemod'])}, "
                            f"{sql_str(n['code'])}, {n['level']}, {n['depth']}, {sql_str(n['parent_id'])}, "
                            f"{sql_str(n['breadcrumb'])}, {n['source_line'] if n['source_line'] is not None else 'NULL'}, "
                            f"{sql_json(n['properties'])})"
                        )
                    sql_parts.append(",\n".join(row_strs) + ";\n")

            if edges_slice:
                sql_parts.append("-- Insert Graph Edges")
                batch_size = 200
                for i in range(0, len(edges_slice), batch_size):
                    batch = edges_slice[i : i + batch_size]
                    sql_parts.append(
                        "INSERT INTO `project_id.dataset_id.xml_graph_edges` "
                        "(edge_id, source_id, destination_id, edge_label, edge_type, ref_text, properties) VALUES"
                    )
                    row_strs = []
                    for e in batch:
                        row_strs.append(
                            f"  ({sql_str(e['edge_id'])}, {sql_str(e['source_id'])}, {sql_str(e['destination_id'])}, "
                            f"{sql_str(e['edge_label'])}, {sql_str(e['edge_type'])}, {sql_str(e['ref_text'])}, "
                            f"{sql_json(e['properties'])})"
                        )
                    sql_parts.append(",\n".join(row_strs) + ";\n")

            return "\n".join(sql_parts), meta

        # Default: 'pg_json' (Property Graph JSON)
        payload = {
            "format": pg["format"],
            "format_version": pg["format_version"],
            "metadata": meta,
            "llm_context": pg["llm_context"],
            "database_ingestion": pg["database_ingestion"],
            "nodes": pg["nodes"],
            "edges": pg["edges"],
        }
        indent = 2 if meta["node_count"] <= 5000 else None
        return json.dumps(payload, indent=indent, ensure_ascii=False), meta

