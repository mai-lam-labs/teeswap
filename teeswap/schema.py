from dataclasses import fields
from typing import Any, override

from litestar._openapi.schema_generation import SchemaCreator
from litestar._openapi.schema_generation.plugins import openapi_schema_plugins
from litestar.openapi.spec import OpenAPIFormat, OpenAPIType, Reference, Schema
from litestar.plugins import OpenAPISchemaPlugin
from litestar.typing import FieldDefinition

from .wire import Validated


class ValidatedSchemaPlugin(OpenAPISchemaPlugin):
    """Litestar maps types by exact match, so our scalar types would get an empty schema.

    Each Validated type states its own schema (json_schema()); this only translates it.
    """

    @override
    def is_plugin_supported_field(self, field_definition: FieldDefinition) -> bool:
        return field_definition.is_subclass_of(Validated)

    @override
    def to_openapi_schema(
        self, field_definition: FieldDefinition, schema_creator: SchemaCreator
    ) -> Schema:
        wire_type = field_definition.annotation
        if not (isinstance(wire_type, type) and issubclass(wire_type, Validated)):
            raise TypeError(f"ValidatedSchemaPlugin cannot handle {wire_type!r}")
        ws = wire_type.json_schema()
        return Schema(
            type=OpenAPIType(ws.type),
            description=ws.description,
            format=OpenAPIFormat(ws.format) if ws.format is not None else None,
            pattern=ws.pattern,
            minimum=float(ws.minimum) if ws.minimum is not None else None,
            maximum=float(ws.maximum) if ws.maximum is not None else None,
        )


# Shared by schema_for_type (MCP inputSchema) and the Litestar app (OpenAPI).
SCHEMA_PLUGINS = [ValidatedSchemaPlugin()]


def schema_for_type(cls: type) -> dict[str, Any]:
    """JSON Schema for a type, as a plain dict (MCP inputSchema)."""
    return schema_object_for_type(cls).to_schema()


def schema_object_for_type(cls: type) -> Schema:
    """The type's schema with every reference inlined, self-contained.

    One source for MCP inputSchema and the OpenAPI request bodies we add in app.py.
    """
    creator = SchemaCreator(plugins=[*SCHEMA_PLUGINS, *openapi_schema_plugins])
    creator.for_field_definition(FieldDefinition.from_annotation(cls))

    registry = {
        "_".join(key): rs.schema for key, rs in creator.schema_registry._schema_key_map.items()
    }
    type_key = "_".join((*cls.__module__.split("."), *cls.__qualname__.split(".")))
    root = registry.get(type_key)
    if root is None:
        raise TypeError(f"no schema generated for {cls.__qualname__}")
    return _inline_schema(root, registry)


def _inline_schema(schema: Schema, registry: dict[str, Schema]) -> Schema:
    for f in fields(schema):
        setattr(schema, f.name, _inline_value(getattr(schema, f.name), registry))
    return schema


def _inline_value(value: object, registry: dict[str, Schema]) -> object:
    if isinstance(value, Reference):
        target = registry.get(value.ref.rsplit("/", 1)[-1])
        return value if target is None else _inline_schema(target, registry)
    if isinstance(value, Schema):
        return _inline_schema(value, registry)
    if isinstance(value, list):
        return [_inline_value(v, registry) for v in value]
    if isinstance(value, tuple):
        return tuple(_inline_value(v, registry) for v in value)
    if isinstance(value, dict):
        return {k: _inline_value(v, registry) for k, v in value.items()}
    return value
