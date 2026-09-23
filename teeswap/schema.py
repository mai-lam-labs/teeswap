from typing import Any, override

from litestar._openapi.schema_generation import SchemaCreator
from litestar._openapi.schema_generation.plugins import openapi_schema_plugins
from litestar.openapi.spec import OpenAPIType, Schema
from litestar.plugins import OpenAPISchemaPlugin
from litestar.typing import FieldDefinition

from .types import HexStr


class HexStrSchemaPlugin(OpenAPISchemaPlugin):
    """Litestar maps types by exact match, so str subclasses otherwise get an empty schema."""

    @override
    def is_plugin_supported_field(self, field_definition: FieldDefinition) -> bool:
        return field_definition.is_subclass_of(HexStr)

    @override
    def to_openapi_schema(
        self, field_definition: FieldDefinition, schema_creator: SchemaCreator
    ) -> Schema:
        hex_type = field_definition.annotation
        if not (isinstance(hex_type, type) and issubclass(hex_type, HexStr)):
            raise TypeError(f"HexStrSchemaPlugin cannot handle {hex_type!r}")
        size = "" if hex_type.LENGTH is None else f"{hex_type.LENGTH}-byte "
        return Schema(
            type=OpenAPIType.STRING,
            pattern=hex_type.pattern(),
            description=f"{size}hex string, 0x-prefixed",
        )


# Shared by schema_for_type (MCP inputSchema) and the Litestar app (OpenAPI).
SCHEMA_PLUGINS = [HexStrSchemaPlugin()]


def schema_for_type(cls: type) -> dict[str, Any]:
    creator = SchemaCreator(plugins=[*SCHEMA_PLUGINS, *openapi_schema_plugins])
    creator.for_field_definition(FieldDefinition.from_annotation(cls))

    all_schemas: dict[str, Any] = {}
    for key, rs in creator.schema_registry._schema_key_map.items():
        ref_name = "_".join(key)
        all_schemas[ref_name] = rs.schema.to_schema()

    module_parts = cls.__qualname__.split(".")
    type_key = "_".join((*cls.__module__.split("."), *module_parts))
    schema = dict(all_schemas.get(type_key, {}))

    _inline_refs(schema, all_schemas)
    return schema


def _inline_refs(obj: Any, registry: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref_name = obj["$ref"].split("/")[-1]
            if ref_name in registry:
                resolved = registry[ref_name]
                obj.clear()
                obj.update(resolved)
                _inline_refs(obj, registry)
            return
        for v in obj.values():
            _inline_refs(v, registry)
    elif isinstance(obj, list):
        for item in obj:
            _inline_refs(item, registry)
