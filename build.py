#!/usr/bin/env python3
"""Rebuild the reading page and EPUB from the published HTML; --check never writes.

Python 3 standard library only. Run from any directory. The website itself needs
no build step. Source HTML repairs are reported, never written back to sources.
"""
import argparse
import copy
import io
import posixpath
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
PUBLIC = "https://yang250423.github.io/-/"
TITLE = "本分：黄峥这个人"
AUTHORS = ("Grok Bot", "拼多多（AI助手）")
BOOK_ID = "urn:uuid:1e76c89b-dd49-5f18-b4fb-ebd9f2ff9230"
EPUB = "本分-黄峥这个人.epub"
XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
CHAPTERS = [path.relative_to(ROOT).as_posix()
            for path in sorted((ROOT / "ch").glob("[0-9][0-9].html")) if path.is_file()]
SOURCES = ["index.html"] + CHAPTERS + ["appendix/weibo.html"]
DEST = {"index.html": ("preface.xhtml", "ch-00"),
        **{name: (f"ch{Path(name).stem}.xhtml", f"ch-{Path(name).stem}") for name in CHAPTERS},
        "appendix/weibo.html": ("appendix.xhtml", "appendix-d")}
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
BLOCK = set("article aside blockquote div dl fieldset figure footer form h1 h2 h3 h4 h5 h6 header hr main nav ol p pre section table ul".split())


def element(tag, text=None, **attrs):
    node = ET.Element(tag, {k.rstrip("_").replace("__", ":"): v for k, v in attrs.items()})
    node.text = text
    return node


def has_class(node, name):
    return name in node.get("class", "").split()


class HTML(HTMLParser):
    """Small tolerant tree builder, including block/paragraph and inline repairs."""
    def __init__(self, source, name):
        super().__init__(convert_charrefs=True)
        self.root = element("document")
        self.stack = [self.root]
        self.issues = []
        self.name = name
        self.feed(source)
        self.close()
        if len(self.stack) > 1:
            self.warn("unclosed at EOF: " + ", ".join(n.tag for n in self.stack[1:]))

    def warn(self, message):
        self.issues.append(f"{self.name}:{self.getpos()[0]}: {message}")

    def handle_starttag(self, tag, attrs):
        tags = [n.tag for n in self.stack]
        if tag in BLOCK and "p" in tags:
            self.warn("implicitly closed <p> before <" + tag + ">")
            self.stack = self.stack[:tags.index("p")]
        if tag == "li" and self.stack[-1].tag == "li":
            self.warn("implicitly closed <li>")
            self.stack.pop()
        node = ET.SubElement(self.stack[-1], tag, {k: v if v is not None else k for k, v in attrs})
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        tags = [n.tag for n in self.stack]
        if tag not in tags[1:]:
            self.warn("unmatched </" + tag + ">")
            return
        index = len(tags) - 1 - tags[::-1].index(tag)
        if index < len(tags) - 1:
            self.warn("closed " + ", ".join(tags[index + 1:]) + " before </" + tag + ">")
        self.stack = self.stack[:index]

    def handle_data(self, data):
        parent = self.stack[-1]
        if len(parent):
            parent[-1].tail = (parent[-1].tail or "") + data
        else:
            parent.text = (parent.text or "") + data


def find(tree, tag, cls):
    return next(n for n in tree.iter(tag) if has_class(n, cls))


def local_link(source, value):
    """Return a repository path/fragment/query, or None for an external URL."""
    absolute = urljoin(PUBLIC + source, value)
    url, base = urlsplit(absolute), urlsplit(PUBLIC)
    if url.scheme != base.scheme or url.netloc != base.netloc or not url.path.startswith(base.path):
        return None
    path = unquote(url.path[len(base.path):])
    path = posixpath.normpath(path)
    if path == "." or url.path.endswith("/"):
        path = ("" if path == "." else path + "/") + "index.html"
    return path, unquote(url.fragment), url.query


def rewrite(node, source, ebook=False):
    node = copy.deepcopy(node)
    for child in node.iter():
        if not ebook and child.get("id"):
            child.set("id", DEST[source][1] + "--" + child.get("id"))
        for attr in ("href", "src"):
            value = child.get(attr)
            if value is None:
                continue
            link = local_link(source, value)
            if link is None:
                continue
            path, fragment, query = link
            if path in DEST and not (path == "index.html" and fragment == "toc"):
                target, anchor = DEST[path]
                value = target + ("#" + quote(fragment) if fragment else "") if ebook else "#" + anchor + ("--" + quote(fragment) if fragment else "")
            elif path in ("book.html", "index.html"):
                if ebook and re.fullmatch(r"ch-\d\d", fragment):
                    value = "preface.xhtml" if fragment == "ch-00" else fragment.replace("-", "") + ".xhtml"
                else:
                    value = "nav.xhtml#toc" if ebook else "#toc"
            else:
                value = PUBLIC + quote(path, safe="/") + ("?" + query if query else "") + ("#" + quote(fragment) if fragment else "")
            child.set(attr, value)
    return node


def serialize(node, xml=False):
    return ET.tostring(node, encoding="unicode", method="xml" if xml else "html")


def xhtml(title, body):
    root = element("html", xmlns=XHTML, **{"xmlns:epub": "http://www.idpf.org/2007/ops", "xml:lang": "zh-CN", "lang": "zh-CN"})
    head = ET.SubElement(root, "head")
    head.append(element("title", title))
    head.append(element("link", rel="stylesheet", type="text/css", href="style.css"))
    ET.SubElement(root, "body").extend(body)
    return ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n" + serialize(root, True)).encode()


def collect():
    if not CHAPTERS or [int(Path(name).stem) for name in CHAPTERS] != list(range(1, len(CHAPTERS) + 1)):
        raise ValueError("ch/ must contain consecutive two-digit chapters starting at 01; found " +
                         (", ".join(Path(name).stem for name in CHAPTERS) or "none"))
    texts = {name: (ROOT / name).read_text(encoding="utf-8") for name in SOURCES}
    parsed = {name: HTML(text, name) for name, text in texts.items()}
    articles = {name: find(doc.root, "article", "preface" if name == "index.html" else "ch") for name, doc in parsed.items()}
    cover = copy.deepcopy(find(parsed["index.html"].root, "section", "cover"))
    for parent in cover.iter():
        for child in list(parent):
            if has_class(child, "cover-links"):
                parent.remove(child)
    entries, group = [("", "index.html", "前言")], ""
    toc = find(parsed["index.html"].root, "ul", "toc")
    for row in toc:
        if has_class(row, "sec"):
            group = "".join(row.itertext()).strip()
        else:
            anchor = row.find("a")
            if anchor is not None and (link := local_link("index.html", anchor.get("href", ""))) and link[0] in DEST and link[0] != "index.html":
                label = " ".join("".join(n.itertext()).strip() for n in anchor)
                entries.append((group, link[0], label))
    if [e[1] for e in entries] != SOURCES:
        raise ValueError(f"index.html must list chapters 01–{len(CHAPTERS):02} and the Weibo appendix once, in order")
    dates = re.findall(r"本站最近更新：(\d{4}-\d\d-\d\d \d\d:\d\d)", "\n".join(texts.values()))
    date = datetime.strptime(max(dates), "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8))) if dates else datetime(2026, 10, 1, tzinfo=timezone.utc)
    return parsed, articles, cover, entries, date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def contents(entries, ebook=False):
    nav = element("nav", class_="book-mini-toc", id="toc", **({"epub:type": "toc"} if ebook else {"aria-label": "全书目录"}))
    nav.append(element("h2", "目录"))
    listing = ET.SubElement(nav, "ol" if ebook else "ul")
    last, current = "", listing
    for group, source, label in entries:
        if group != last:
            row = ET.SubElement(listing, "li", {"class": "sec"})
            row.append(element("span", group))
            current = ET.SubElement(row, "ol") if ebook else listing
            last = group
        ET.SubElement(current, "li").append(element("a", label, href=DEST[source][0] if ebook else "#" + DEST[source][1]))
    return nav


def generate():
    parsed, articles, cover, entries, modified = collect()
    full = element("article", class_="book-full")
    full.append(copy.deepcopy(cover))
    ET.SubElement(full, "p", {"class": "cover-links"}).append(element("a", "下载 EPUB", href=quote(EPUB), download="download"))
    full.append(contents(entries))
    for _, source, label in entries:
        section = ET.SubElement(full, "section", {"class": "ch-block", "id": DEST[source][1]})
        section.append(element("p", label, class_="ch-run"))
        article = rewrite(articles[source], source)
        for nav in list(article.findall("nav")):
            if has_class(nav, "nav-bottom"):
                article.remove(nav)
        section.append(article)
    page = "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>全书通读 · " + TITLE + "</title><link rel=\"stylesheet\" href=\"style.css\"></head><body><div class=\"wrap\">" + serialize(full) + "</div></body></html>\n"
    files = {"mimetype": b"application/epub+zip", "META-INF/container.xml": b'<?xml version="1.0" encoding="utf-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'}
    docs = {"titlepage.xhtml": xhtml(TITLE, [cover]), "nav.xhtml": xhtml("目录", [contents(entries, True)])}
    for _, source, label in entries:
        docs[DEST[source][0]] = xhtml(label, [rewrite(articles[source], source, True)])
    package = element("package", xmlns=OPF, version="3.0", **{"unique-identifier": "bookid", "xml:lang": "zh-CN"})
    metadata = ET.SubElement(package, "metadata", {"xmlns:dc": "http://purl.org/dc/elements/1.1/"})
    metadata.extend([element("dc:identifier", BOOK_ID, id="bookid"), element("dc:title", TITLE)])
    metadata.extend(element("dc:creator", author, id=f"au{n}") for n, author in enumerate(AUTHORS))
    metadata.extend([element("dc:language", "zh-CN"), element("dc:publisher", "本分读本（静态站）"), element("dc:date", "2026-10"), element("meta", modified, property="dcterms:modified")])
    manifest = ET.SubElement(package, "manifest")
    for name in ["nav.xhtml", "toc.ncx", "style.css", "titlepage.xhtml"] + [DEST[s][0] for s in SOURCES]:
        attrs = {"id": name.split(".")[0], "href": name, "media-type": "text/css" if name.endswith(".css") else "application/x-dtbncx+xml" if name.endswith(".ncx") else "application/xhtml+xml"}
        if name == "nav.xhtml":
            attrs["properties"] = "nav"
        ET.SubElement(manifest, "item", attrs)
    spine = ET.SubElement(package, "spine", {"toc": "toc"})
    for name in ["titlepage.xhtml"] + [DEST[s][0] for s in SOURCES]:
        ET.SubElement(spine, "itemref", {"idref": name.split(".")[0]})
    ncx = element("ncx", xmlns="http://www.daisy.org/z3986/2005/ncx/", version="2005-1")
    ET.SubElement(ncx, "head").append(element("meta", name="dtb:uid", content=BOOK_ID))
    ET.SubElement(ncx, "docTitle").append(element("text", TITLE))
    navmap = ET.SubElement(ncx, "navMap")
    for n, (_, source, label) in enumerate(entries, 1):
        point = ET.SubElement(navmap, "navPoint", {"id": f"np{n}", "playOrder": str(n)})
        ET.SubElement(point, "navLabel").append(element("text", label))
        point.append(element("content", src=DEST[source][0]))
    docs.update({"content.opf": serialize(package, True).encode(), "toc.ncx": serialize(ncx, True).encode(), "style.css": (ROOT / "style.css").read_bytes()})
    files.update({"OEBPS/" + name: data for name, data in docs.items()})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data, compresslevel=9)
    return {"book.html": page.encode(), EPUB: archive.getvalue()}, [issue for doc in parsed.values() for issue in doc.issues]


def validate_links(trees, errors, epub=False):
    ids = {name: {n.get("id") for n in tree.iter() if n.get("id")} for name, tree in trees.items()}
    for name, tree in trees.items():
        seen = set()
        for node in tree.iter():
            anchor = node.get("id")
            if anchor and anchor in seen:
                errors.append(f"{name}: duplicate id {anchor}")
            seen.add(anchor)
            for attr in ("href", "src"):
                value = node.get(attr)
                if value is None:
                    continue
                if epub:
                    url = urlsplit(value)
                    if url.scheme or url.netloc:
                        continue
                    path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(url.path))) if url.path else name
                    fragment = unquote(url.fragment)
                    exists = path in trees or path == "OEBPS/style.css"
                else:
                    link = local_link(name, value)
                    if link is None:
                        continue
                    path, fragment, _ = link
                    exists = (ROOT / path).is_file() and (ROOT / path).resolve().is_relative_to(ROOT)
                if not exists:
                    errors.append(f"{name}: missing local target {value}")
                elif fragment and path in ids and fragment not in ids[path]:
                    errors.append(f"{name}: missing anchor {value}")


def validate_epub(data, errors):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if len(set(names)) != len(names) or z.testzip():
                errors.append("EPUB: duplicate entries or failed CRC")
            if names[0] != "mimetype" or z.getinfo("mimetype").compress_type != zipfile.ZIP_STORED or z.read("mimetype") != b"application/epub+zip":
                errors.append("EPUB: invalid mimetype entry")
            trees = {name: ET.fromstring(z.read(name)) for name in names if name.endswith((".xml", ".opf", ".ncx", ".xhtml"))}
            container = trees["META-INF/container.xml"]
            opf_name = container.find(".//{*}rootfile").get("full-path")
            package = trees[opf_name]
            items = package.findall("{*}manifest/{*}item")
            manifest = {n.get("id"): n for n in items}
            paths = [posixpath.join(posixpath.dirname(opf_name), n.get("href")) for n in items]
            if len(manifest) != len(items) or len(set(paths)) != len(paths) or any(p not in names for p in paths):
                errors.append("EPUB: invalid manifest IDs, paths, or missing files")
            if set(paths) != set(names) - {"mimetype", "META-INF/container.xml", opf_name}:
                errors.append("EPUB: manifest does not cover archive contents")
            spine = [n.get("idref") for n in package.findall("{*}spine/{*}itemref")]
            if spine != ["titlepage"] + [DEST[s][0].split(".")[0] for s in SOURCES]:
                errors.append("EPUB: unexpected or missing spine entries")
            if not any("nav" in n.get("properties", "").split() for n in items):
                errors.append("EPUB: navigation document missing")
            if any(trees[p].tag != "{" + XHTML + "}html" for p in paths if p.endswith(".xhtml")):
                errors.append("EPUB: XHTML namespace missing")
            validate_links(trees, errors, epub=True)
    except (ValueError, KeyError, IndexError, AttributeError, ET.ParseError, zipfile.BadZipFile) as exc:
        errors.append("EPUB: " + str(exc))


def same_output(name, actual, expected):
    if name != EPUB:
        return actual == expected
    # Compare packaged contents, not the compression stream: zlib versions on
    # macOS and GitHub's Linux runner can encode identical payloads differently.
    try:
        with zipfile.ZipFile(io.BytesIO(actual)) as a, zipfile.ZipFile(io.BytesIO(expected)) as b:
            return a.namelist() == b.namelist() and all(a.read(n) == b.read(n) for n in b.namelist())
    except (zipfile.BadZipFile, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="read-only: compare generated outputs and validate local links and EPUB")
    args = parser.parse_args()
    expected, warnings = generate()
    for warning in warnings:
        print("HTML repair: " + warning, file=sys.stderr)
    errors = []
    validate_epub(expected[EPUB], errors)
    if args.check:
        for name, data in expected.items():
            if not (ROOT / name).is_file() or not same_output(name, (ROOT / name).read_bytes(), data):
                errors.append(name + ": missing or out of date; run python3 build.py")
        trees = {p.relative_to(ROOT).as_posix(): HTML(p.read_text(encoding="utf-8"), p.relative_to(ROOT).as_posix()).root for p in ROOT.rglob("*.html") if ".git" not in p.parts}
        validate_links(trees, errors)
        if (ROOT / EPUB).is_file():
            validate_epub((ROOT / EPUB).read_bytes(), errors)
    if errors:
        print("\n".join("ERROR: " + error for error in errors), file=sys.stderr)
        return 1
    if not args.check:
        for name, data in expected.items():
            (ROOT / name).write_bytes(data)
    print(("Check passed" if args.check else "Built book.html and " + EPUB) + f"; {len(warnings)} source HTML repair(s) reported.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, StopIteration) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
