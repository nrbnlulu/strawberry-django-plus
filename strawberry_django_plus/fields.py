import dataclasses
from functools import cached_property
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import QuerySet
from django.db.models.fields.reverse_related import ManyToManyRel, ManyToOneRel
from django.db.models.query import Prefetch
from django.db.models.query_utils import DeferredAttribute
from strawberry.annotation import StrawberryAnnotation
from strawberry.arguments import UNSET, is_unset
from strawberry.permission import BasePermission
from strawberry.schema_directive import StrawberrySchemaDirective
from strawberry.types.fields.resolver import StrawberryResolver
from strawberry.types.info import Info
from strawberry_django.fields.field import (
    StrawberryDjangoField as _StrawberryDjangoField,
)
from strawberry_django.fields.types import (
    get_model_field,
    is_auto,
    is_optional,
    resolve_model_field_name,
    resolve_model_field_type,
)
from strawberry_django.utils import is_similar_django_type
from typing_extensions import Self

from .descriptors import ModelProperty
from .optimizer import DjangoOptimizerStore, TypeOrSequence
from .resolvers import qs_resolver, resolve_getattr, resolve_result

if TYPE_CHECKING:
    from .types import StrawberryDjangoType

_T = TypeVar("_T")
_M = TypeVar("_M", bound=models.Model)


class StrawberryDjangoField(_StrawberryDjangoField):
    store: DjangoOptimizerStore

    def __init__(self, *args, **kwargs):
        self.store = DjangoOptimizerStore.from_iterables(
            only=kwargs.pop("only", None),
            select_related=kwargs.pop("select_related", None),
            prefetch_related=kwargs.pop("prefetch_related", None),
        )
        super().__init__(*args, **kwargs)

    @cached_property
    def model(self) -> Type[models.Model]:
        model = self.django_model
        if model:
            return model

        origin = self.origin_django_type or self.origin._django_type  # type:ignore
        return origin.model

    @cached_property
    def model_pk(self):
        pk = self.model._meta.pk
        assert pk
        return pk

    @classmethod
    def from_django_type(
        cls,
        django_type: "StrawberryDjangoType",
        name: str,
        *,
        type_annotation: Optional[StrawberryAnnotation] = None,
    ) -> Self:
        origin = django_type.origin

        attr = getattr(origin, name, UNSET)
        if is_unset(attr):
            attr = getattr(cls, "__dataclass_fields__", {}).get(name, UNSET)
        if attr is dataclasses.MISSING:
            attr = UNSET

        if isinstance(attr, cls) and not attr.origin_django_type:
            field = cast(Self, attr)
        elif isinstance(attr, dataclasses.Field):
            default = getattr(attr, "default", UNSET)
            if default is dataclasses.MISSING:
                default = UNSET

            default_factory = getattr(attr, "default_factory", UNSET)
            if default_factory is dataclasses.MISSING:
                default_factory = UNSET

            if type_annotation is None:
                type_annotation = getattr(attr, "type_annotation", None)
            if type_annotation is None:
                type_annotation = StrawberryAnnotation(attr.type)

            field = cls(
                django_name=getattr(attr, "django_name", attr.name),
                graphql_name=getattr(attr, "graphql_name", None),
                origin=getattr(attr, "origin", None),
                is_subscription=getattr(attr, "is_subscription", False),
                description=getattr(attr, "description", None),
                base_resolver=getattr(attr, "base_resolver", None),
                permission_classes=getattr(attr, "permission_classes", ()),
                default=default,
                default_factory=default_factory,
                deprecation_reason=getattr(attr, "deprecation_reason", None),
                directives=getattr(attr, "directives", ()),
                type_annotation=type_annotation,
            )
        elif isinstance(attr, StrawberryResolver):
            field = cls(base_resolver=attr)
        elif callable(attr):
            field = cast(Self, cls()(attr))
        else:
            field = cls(default=attr)

        field.python_name = name
        # store origin django type for further usage
        if name in origin.__dict__.get("__annotations__", {}):
            field.origin_django_type = django_type

        # annotation of field is used as a class type
        if type_annotation is not None:
            field.type_annotation = type_annotation
            field.is_auto = is_auto(field.type_annotation)

        # resolve the django_name and check if it is relation field. django_name
        # is used to access the field data in resolvers
        try:
            model_field = get_model_field(django_type.model, field.django_name or name)
        except FieldDoesNotExist:
            model_attr = getattr(django_type.model, name, None)
            if model_attr is not None and isinstance(model_attr, ModelProperty):
                if field.is_auto:
                    field.type_annotation = StrawberryAnnotation(model_attr.type_annotation)
                    field.is_auto = is_auto(field.type_annotation)

                if field.description is None:
                    field.description = model_attr.description
            elif field.django_name or field.is_auto:
                raise  # field should exist, reraise caught exception
        else:
            field.is_relation = model_field.is_relation
            field.django_name = resolve_model_field_name(
                model_field,
                is_input=django_type.is_input,
                is_filter=django_type.is_filter,
            )

            # change relation field type to auto if field is inherited from another
            # type. for example if field is inherited from output type but we are
            # configuring field for input type
            if field.is_relation and not is_similar_django_type(
                django_type, field.origin_django_type
            ):
                field.is_auto = True

            # resolve type of auto field
            if field.is_auto:
                field_type = resolve_model_field_type(model_field, django_type)
                field.type_annotation = StrawberryAnnotation(field_type)

            if is_optional(model_field, django_type.is_input, django_type.is_partial):
                assert field.type_annotation
                field.type_annotation.annotation = Optional[
                    field.type_annotation.annotation  # type:ignore
                ]

            if field.description is None:
                field.description = (
                    model_field.field.help_text
                    if isinstance(model_field, (ManyToOneRel, ManyToManyRel))
                    else model_field.help_text
                )

        return field

    def get_result(
        self,
        source: Optional[models.Model],
        info: Info,
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> Union[Awaitable[Any], Any]:
        if self.base_resolver is not None:
            # Unlike strawberry_django, we don't enforce this on sync_to_async since it adds
            # a lot of overhead which might be unnecessary. Leave it up to the implementation
            # to decide what to do...
            result = self.base_resolver(*args, **kwargs)
        elif source is None:
            result = self.model.objects.all()
        else:
            # Small optimization to async resolvers avoid having to call it in an sync_to_async
            # context if the value is already cached, since it will not hit the db anymore
            attname = self.django_name or self.python_name
            attr = getattr(source.__class__, attname, None)
            try:
                if isinstance(attr, DeferredAttribute):
                    result = source.__dict__[attr.field.attname]
                elif isinstance(attr, ModelProperty):
                    result = source.__dict__[attr.name]
                else:
                    raise KeyError
            except KeyError:
                result = resolve_getattr(source, self.django_name or self.python_name)

        if self.is_list:
            qs_resolver = lambda qs: self.get_list(info, qs, **kwargs)
        else:
            qs_resolver = lambda qs: self.get_one(info, qs, **kwargs)

        return resolve_result(result, info, resolve_callable_func=qs_resolver)

    @qs_resolver(get_list=True)
    def get_list(self, info: Info, qs: QuerySet[Any], **kwargs) -> QuerySet[Any]:
        # The qs_resolver will ensure this returns a list
        return self.get_queryset(qs, info, **kwargs)

    @qs_resolver(get_one=True)
    def get_one(self, info: Info, qs: QuerySet[Any], **kwargs) -> QuerySet[Any]:
        # The qs_resolver will ensure this returns a single result
        return self.get_queryset(qs, info, **kwargs)


@overload
def field(
    *,
    resolver: Callable[[], _T],
    name: Optional[str] = None,
    field_name: Optional[str] = None,
    filters: Any = UNSET,
    is_subscription: bool = False,
    description: Optional[str] = None,
    init: Literal[False] = False,
    permission_classes: Optional[List[Type[BasePermission]]] = None,
    deprecation_reason: Optional[str] = None,
    default: Any = UNSET,
    default_factory: Union[Callable, object] = UNSET,
    directives: Optional[Sequence[StrawberrySchemaDirective]] = (),
    only: Optional[TypeOrSequence[str]] = None,
    select_related: Optional[TypeOrSequence[str]] = None,
    prefetch_related: Optional[TypeOrSequence[Union[str, Prefetch]]] = None,
) -> _T:
    ...


@overload
def field(
    *,
    name: Optional[str] = None,
    field_name: Optional[str] = None,
    filters: Any = UNSET,
    is_subscription: bool = False,
    description: Optional[str] = None,
    init: Literal[True] = True,
    permission_classes: Optional[List[Type[BasePermission]]] = None,
    deprecation_reason: Optional[str] = None,
    default: Any = UNSET,
    default_factory: Union[Callable, object] = UNSET,
    directives: Optional[Sequence[StrawberrySchemaDirective]] = (),
    only: Optional[TypeOrSequence[str]] = None,
    select_related: Optional[TypeOrSequence[str]] = None,
    prefetch_related: Optional[TypeOrSequence[Union[str, Prefetch]]] = None,
) -> Any:
    ...


@overload
def field(
    resolver: Union[StrawberryResolver, Callable, staticmethod, classmethod],
    *,
    name: Optional[str] = None,
    field_name: Optional[str] = None,
    filters: Any = UNSET,
    is_subscription: bool = False,
    description: Optional[str] = None,
    permission_classes: Optional[List[Type[BasePermission]]] = None,
    deprecation_reason: Optional[str] = None,
    default: Any = UNSET,
    default_factory: Union[Callable, object] = UNSET,
    directives: Optional[Sequence[StrawberrySchemaDirective]] = (),
    only: Optional[TypeOrSequence[str]] = None,
    select_related: Optional[TypeOrSequence[str]] = None,
    prefetch_related: Optional[TypeOrSequence[Union[str, Prefetch]]] = None,
) -> StrawberryDjangoField:
    ...


def field(
    resolver=None,
    *,
    name: Optional[str] = None,
    field_name: Optional[str] = None,
    filters: Any = UNSET,
    is_subscription: bool = False,
    description: Optional[str] = None,
    permission_classes: Optional[List[Type[BasePermission]]] = None,
    deprecation_reason: Optional[str] = None,
    default: Any = UNSET,
    default_factory: Union[Callable, object] = UNSET,
    directives: Optional[Sequence[StrawberrySchemaDirective]] = (),
    only: Optional[TypeOrSequence[str]] = None,
    select_related: Optional[TypeOrSequence[str]] = None,
    prefetch_related: Optional[TypeOrSequence[Union[str, Prefetch]]] = None,
    # This init parameter is used by pyright to determine whether this field
    # is added in the constructor or not. It is not used to change
    # any behavior at the moment.
    init=None,
) -> Any:
    f = StrawberryDjangoField(
        python_name=None,
        django_name=field_name,
        graphql_name=name,
        type_annotation=None,
        description=description,
        is_subscription=is_subscription,
        permission_classes=permission_classes or [],
        deprecation_reason=deprecation_reason,
        default=default,
        default_factory=default_factory,
        directives=directives,
        only=only,
        select_related=select_related,
        prefetch_related=prefetch_related,
    )
    if resolver:
        f = f(resolver)
    return f