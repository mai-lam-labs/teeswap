"""Cleanroom typed HTML builder — replaces dominate with full type annotations."""

from __future__ import annotations

from contextvars import ContextVar
from html import escape
from typing import ClassVar, Self, override

_current_parent: ContextVar[Element | None] = ContextVar("_current_parent", default=None)


class Node:
    """Abstract base — anything renderable."""

    @override
    def __str__(self) -> str:
        raise NotImplementedError


class TextNode(Node):
    """HTML-escaped text content."""

    __slots__ = ("_content",)

    def __init__(self, content: str) -> None:
        self._content = content

    @override
    def __str__(self) -> str:
        return escape(self._content)


class RawNode(Node):
    """Unescaped HTML content (for inline CSS/JS)."""

    __slots__ = ("_content",)

    def __init__(self, content: str) -> None:
        self._content = content

    @override
    def __str__(self) -> str:
        return self._content


def _clean_attrs(**attrs: str | bool) -> dict[str, str]:
    """Normalise attribute names and values.

    - ``cls`` → ``class``
    - trailing ``_`` stripped (e.g. ``for_`` → ``for``)
    - ``True`` → attribute name as value (boolean attribute)
    - ``False`` → omitted
    """
    out: dict[str, str] = {}
    for key, val in attrs.items():
        if val is False:
            continue
        # Rename cls → class; strip trailing _; convert _ to - in data attrs
        if key == "cls":
            key = "class"
        elif key.endswith("_"):
            key = key[:-1]
        if key.startswith("data_"):
            key = key.replace("_", "-")
        if val is True:
            out[key] = key
        else:
            out[key] = str(val)
    return out


def _render_attrs(attrs: dict[str, str]) -> str:
    if not attrs:
        return ""
    parts = [f' {k}="{escape(v, quote=True)}"' for k, v in attrs.items()]
    return "".join(parts)


class Element(Node):
    """HTML element with tag, attributes, children."""

    _tag_name: ClassVar[str] = ""
    _void: ClassVar[bool] = False

    __slots__ = ("_attrs", "_token", "children")

    def __init__(
        self,
        *children: str | Node,
        _auto_append: bool = True,
        **attrs: str | bool,
    ) -> None:
        self._attrs = _clean_attrs(**attrs)
        self.children: list[Node] = []
        for child in children:
            self.children.append(TextNode(child) if isinstance(child, str) else child)
        if _auto_append:
            parent = _current_parent.get(None)
            if parent is not None:
                parent.children.append(self)

    def __enter__(self: Self) -> Self:
        self._token = _current_parent.set(self)
        return self

    def __exit__(self, *exc: object) -> None:
        _current_parent.reset(self._token)

    def __setitem__(self, key: str, value: str) -> None:
        if key == "cls":
            key = "class"
        elif key.endswith("_"):
            key = key[:-1]
        self._attrs[key] = value

    @override
    def __str__(self) -> str:
        attr_str = _render_attrs(self._attrs)
        if self._void:
            return f"<{self._tag_name}{attr_str}>"
        inner = "".join(str(c) for c in self.children)
        return f"<{self._tag_name}{attr_str}>{inner}</{self._tag_name}>"


class _RawContentElement(Element):
    """For <style>/<script> — wraps string children in RawNode instead of TextNode."""

    def __init__(
        self,
        *children: str | Node,
        _auto_append: bool = True,
        **attrs: str | bool,
    ) -> None:
        raw_children: list[str | Node] = [RawNode(c) if isinstance(c, str) else c for c in children]
        super().__init__(*raw_children, _auto_append=_auto_append, **attrs)


# --- Tag subclasses ---


# Block elements
class div(Element):
    _tag_name = "div"


class p(Element):
    _tag_name = "p"


class h1(Element):
    _tag_name = "h1"


class h2(Element):
    _tag_name = "h2"


class h3(Element):
    _tag_name = "h3"


class nav(Element):
    _tag_name = "nav"


class form(Element):
    _tag_name = "form"


# Table elements
class table(Element):
    _tag_name = "table"


class thead(Element):
    _tag_name = "thead"


class tbody(Element):
    _tag_name = "tbody"


class tr(Element):
    _tag_name = "tr"


class th(Element):
    _tag_name = "th"


class td(Element):
    _tag_name = "td"


# Inline elements
class span(Element):
    _tag_name = "span"


class a(Element):
    _tag_name = "a"


class label(Element):
    _tag_name = "label"


# Form elements
class select(Element):
    _tag_name = "select"


class optgroup(Element):
    _tag_name = "optgroup"


class option(Element):
    _tag_name = "option"


class button(Element):
    _tag_name = "button"


class input_(Element):
    _tag_name = "input"
    _void = True


# Head elements
class meta(Element):
    _tag_name = "meta"
    _void = True


class style(_RawContentElement):
    _tag_name = "style"


class script(_RawContentElement):
    _tag_name = "script"


# Misc
class br(Element):
    _tag_name = "br"
    _void = True


# Private subclasses for document structure
class _head(Element):
    _tag_name = "head"


class _body(Element):
    _tag_name = "body"


class _title(Element):
    _tag_name = "title"


class document:
    """Full HTML document with head and body."""

    def __init__(self, title: str = "Dominate") -> None:
        self.head = _head(_auto_append=False)
        self.title = _title(title, _auto_append=False)
        self.head.children.append(self.title)
        self.body = _body(_auto_append=False)

    @override
    def __str__(self) -> str:
        return f"<!DOCTYPE html>\n<html>\n{self.head}\n{self.body}\n</html>"


def text(content: str) -> TextNode:
    """Create an HTML-escaped text node, auto-appended to the current parent."""
    node = TextNode(content)
    parent = _current_parent.get(None)
    if parent is not None:
        parent.children.append(node)
    return node


def raw(content: str) -> RawNode:
    """Create an unescaped HTML node, auto-appended to the current parent."""
    node = RawNode(content)
    parent = _current_parent.get(None)
    if parent is not None:
        parent.children.append(node)
    return node
