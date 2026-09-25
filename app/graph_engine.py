from __future__ import annotations

import os
import time
from collections import Counter, defaultdict
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

    def get_summary(self) -> Dict[str, Any]:
        return {
            "xml_filename": self.xml_path.name,
            "xml_size_bytes": self.xml_path.stat().st_size,
            "xsd_filename": self.xsd_path.name,
            "is_valid": self.is_valid,
            "validation_errors": self.validation_errors,
            "load_time_ms": self.load_time_ms,
            "total_graph_nodes": len(self.nodes),
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
    ) -> Dict[str, Any]:
        highlight_ids = highlight_ids or set()
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
        self, query: str, max_results: int = 25, include_children: bool = True
    ) -> Dict[str, Any]:
        q = query.strip()
        if not q:
            return self.get_initial_subgraph()

        q_lower = q.lower()
        q_upper = q.upper()

        matched_ids: List[str] = []

        # 1. Exact ICD-10 code match (e.g., "Q27.8" or "F44.4")
        if q_upper in self.code_to_node_id:
            matched_ids = [self.code_to_node_id[q_upper]]
        else:
            # 2. Term search: prioritize exact title match first!
            exact_matches: List[str] = []
            starts_with: List[str] = []
            contains: List[str] = []

            for nid, ndata in self.nodes.items():
                if ndata["tag"] in ("ICD10CM.index", "letter", "code", "see", "seeAlso", "manif", "seecat", "subcat"):
                    continue
                title_val = (ndata.get("title") or "").strip()
                title_lower = title_val.lower()
                full_title_lower = f"{title_val} {ndata.get('nemod', '')}".strip().lower()

                if title_lower == q_lower or full_title_lower == q_lower:
                    exact_matches.append(nid)
                elif not exact_matches:
                    if title_lower.startswith(q_lower):
                        if len(starts_with) < max_results:
                            starts_with.append(nid)
                    elif q_lower in title_lower and len(starts_with) == 0 and len(contains) < max_results:
                        contains.append(nid)

            if exact_matches:
                # Prefer mainTerm exact matches if available, otherwise all exact matches up to max_results
                main_term_exact = [nid for nid in exact_matches if self.nodes[nid]["tag"] == "mainTerm"]
                matched_ids = (main_term_exact or exact_matches)[:max_results]
            elif starts_with:
                matched_ids = starts_with[:max_results]
            else:
                matched_ids = contains[:max_results]

        if not matched_ids:
            return {
                "mode": "document",
                "nodes": [],
                "edges": [],
                "node_count": 0,
                "edge_count": 0,
                "matched_count": 0,
                "query": query,
            }

        # Build the strict set of:
        # (a) matched nodes M
        # (b) nodes directly connected to M (or M's subterm hierarchy)
        # (c) the upward ancestor hierarchy of M and its directly connected nodes
        core_nodes: Set[str] = set(matched_ids)
        allowed_semantic_edges: Set[str] = set()

        def collect_subterm_hierarchy(parent_nid: str) -> None:
            for cid in self.parent_to_children.get(parent_nid, []):
                core_nodes.add(cid)
                # Direct semantic targets (excluding SEE_ALSO) of this subterm
                for sedge in self.node_semantic_edges.get(cid, []):
                    if sedge.get("edge_type") == "SEE_ALSO":
                        continue
                    if sedge["source"] == cid:
                        core_nodes.add(sedge["target"])
                        allowed_semantic_edges.add(sedge["id"])
                collect_subterm_hierarchy(cid)

        for mid in matched_ids:
            mnode = self.nodes.get(mid, {})
            # Direct parent of mid
            if mnode.get("parent_id"):
                core_nodes.add(mnode["parent_id"])

            # Direct semantic connections of mid (excluding SEE_ALSO edges)
            for sedge in self.node_semantic_edges.get(mid, []):
                if sedge.get("edge_type") == "SEE_ALSO":
                    continue
                if mnode.get("tag") in ("mainTerm", "term", "entity") and sedge["source"] != mid:
                    continue
                core_nodes.add(sedge["source"])
                core_nodes.add(sedge["target"])
                allowed_semantic_edges.add(sedge["id"])

            # Downward subterm hierarchy of mid (if mid is a term/mainTerm)
            if include_children and mnode.get("tag") in ("mainTerm", "term", "entity"):
                collect_subterm_hierarchy(mid)

        # Add upward ancestor hierarchy (up to ICD10CM.index root) for all included nodes
        included = self._add_ancestor_hierarchy(core_nodes)

        res = self._build_subgraph_payload(
            included,
            include_semantic=False,
            highlight_ids=set(matched_ids),
            allowed_semantic_edge_ids=allowed_semantic_edges,
        )
        res["matched_count"] = len(matched_ids)
        res["matched_ids"] = matched_ids
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
            for cid in self.parent_to_children.get(node_id, [])[:30]
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
                for tid in self.code_to_terms[node_id][:30]
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
