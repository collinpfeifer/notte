import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Callable, ClassVar, Required, TypeAlias, TypeVar

from loguru import logger
from typing_extensions import TypedDict

from notte.browser.node_type import NodeCategory, NodeRole, NodeType
from notte.errors.processing import (
    InvalidInternalCheckError,
    NodeFilteringResultsInEmptyGraph,
)

T = TypeVar("T", bound="DomNode")  # T must be a subclass of DomNode


class A11yNode(TypedDict, total=False):
    # from the a11y tree
    role: Required[str]
    name: Required[str]
    children: list["A11yNode"]
    url: str
    # added by the tree processing
    only_text_roles: bool
    nb_pruned_children: int
    children_roles_count: dict[str, int]
    group_role: str
    group_roles: list[str]
    markdown: str
    # added by the notte processing
    id: str
    path: str  # url:parent-path:role:name
    # stuff for the action listing
    modal: bool
    required: bool
    description: str
    visible: bool
    selected: bool
    checked: bool
    enabled: bool

    is_interactive: bool


@dataclass
class A11yTree:
    raw: A11yNode
    simple: A11yNode


@dataclass(frozen=True)
class NodeSelectors:
    css_selector: str
    xpath_selector: str
    notte_selector: str
    in_iframe: bool
    in_shadow_root: bool
    iframe_parent_css_selectors: list[str]
    playwright_selector: str | None = None

    def selectors(self) -> list[str]:
        l: list[str] = []
        if self.playwright_selector is not None:
            l.append(self.playwright_selector)
        l.append(self.css_selector)
        l.append(self.xpath_selector)
        return l


# Type alias for clarity
AttributeValues: TypeAlias = list[str | int | bool | None]


class DomErrorBuffer:
    """Buffer for DOM attribute errors to avoid spam logging."""

    _buffer: ClassVar[dict[str, AttributeValues]] = {}
    _max_samples_per_key: ClassVar[int] = 5

    @staticmethod
    def add_error(extra_keys: set[str], values: dict[str, AttributeValues]) -> None:
        """
        Add an error to the buffer, consolidating the values.
        Each attribute will store up to _max_samples_per_key unique values.
        """

        for key in extra_keys:
            if key not in DomErrorBuffer._buffer.keys():
                DomErrorBuffer._buffer[key] = []
            str_v = str(values[key])[:50]
            if (
                len(DomErrorBuffer._buffer[key]) < DomErrorBuffer._max_samples_per_key
                and str_v not in DomErrorBuffer._buffer[key]
            ):
                DomErrorBuffer._buffer[key].append(str_v)

    @staticmethod
    def flush() -> None:
        """Flush all buffered error messages in a consolidated format."""
        if len(DomErrorBuffer._buffer) == 0:
            return

        logger.error(
            f"""
Extra DOM attributes found: {list(DomErrorBuffer._buffer.keys())}.
Sample values:
{DomErrorBuffer._buffer}
These attributes should be added to the DomAttributes class. Fix this ASAP.
"""
        )
        # Clear the buffer
        DomErrorBuffer._buffer.clear()


@dataclass
class DomAttributes:
    # State attributes
    modal: bool | None
    required: bool | None
    visible: bool | None
    selected: bool | None
    checked: bool | None
    enabled: bool | None
    focused: bool | None
    disabled: bool | None
    pressed: bool | None
    type: str | None

    # Value attributes
    value: str | None
    valuemin: str | None
    valuemax: str | None
    description: str | None
    autocomplete: str | None
    haspopup: bool | None
    accesskey: str | None
    autofocus: bool | None
    tabindex: int | None
    multiselectable: bool | None

    # HTML element attributes
    tag_name: str
    class_name: str | None

    # Resource attributes
    href: str | None
    src: str | None
    srcset: str | None
    target: str | None
    ping: str | None
    data_src: str | None
    data_srcset: str | None

    # Text attributes
    placeholder: str | None
    title: str | None
    alt: str | None
    name: str | None
    autocorrect: str | None
    autocapitalize: str | None
    spellcheck: bool | None
    maxlength: int | None

    # Layout attributes
    width: int | None
    height: int | None
    size: int | None
    rows: int | None

    # Internationalization attributes
    lang: str | None
    dir: str | None

    # aria attributes
    action: str | None
    role: str | None
    aria_label: str | None
    aria_labelledby: str | None
    aria_describedby: str | None
    aria_hidden: bool | None
    aria_expanded: bool | None
    aria_controls: str | None
    aria_haspopup: bool | None
    aria_current: str | None
    aria_autocomplete: str | None
    aria_selected: bool | None
    aria_modal: bool | None
    aria_disabled: bool | None
    aria_valuenow: int | None
    aria_live: str | None
    aria_atomic: bool | None
    aria_valuemax: int | None
    aria_valuemin: int | None
    aria_level: int | None
    aria_owns: str | None
    aria_multiselectable: bool | None
    aria_colindex: int | None
    aria_colspan: int | None
    aria_rowindex: int | None
    aria_rowspan: int | None
    aria_description: str | None
    aria_activedescendant: str | None
    hidden: bool | None
    expanded: bool | None

    @staticmethod
    def init(**kwargs) -> "DomAttributes":
        # compute additional attributes
        if "class" in kwargs:
            kwargs["class_name"] = kwargs["class"]
            del kwargs["class"]

        # replace '-' with '_' in keys
        kwargs = {
            k.replace("-", "_"): v
            for k, v in kwargs.items()
            if (
                not k.startswith("data-")
                and not k.startswith("js")
                and not k.startswith("__")
                and not k.startswith("g-")
            )
        }

        keys = set(DomAttributes.__dataclass_fields__.keys())
        excluded_keys = set(
            [
                "browser_user_highlight_id",
                "class",
                "style",
                "id",
                "data_jsl10n",
                "keyshortcuts",
                "for",
                "rel",
                "ng_non_bindable",
                "c_wiz",
                "ssk",
                "soy_skip",
                "key",
                "method",
                "eid",
                "view",
                "pivot",
            ]
        )

        extra_keys = set(kwargs.keys()).difference(keys).difference(excluded_keys)
        if len(extra_keys) > 0:
            DomErrorBuffer.add_error(extra_keys, kwargs)

        return DomAttributes(**{key: kwargs.get(key, None) for key in keys})

    def relevant_attrs(
        self,
        include_attributes: frozenset[str] | None = None,
        max_len_per_attribute: int | None = None,
    ) -> dict[str, str | bool | int]:
        disabled_attrs = set(
            [
                "tag_name",
                "class_name",
                "width",
                "height",
                "size",
                "lang",
                "dir",
                "action",
                "role",
                "aria_label",
                "name",
            ]
        ).difference(include_attributes or frozenset())
        dict_attrs = asdict(self)
        attrs: dict[str, str | bool | int] = {}
        for key, value in dict_attrs.items():
            if (
                key not in disabled_attrs
                and (include_attributes is None or key in include_attributes)
                and value is not None
            ):
                if max_len_per_attribute is not None and isinstance(value, str) and len(value) > max_len_per_attribute:
                    value = value[:max_len_per_attribute] + "..."
                attrs[key] = value
        return attrs

    @staticmethod
    def from_a11y_node(node: A11yNode) -> "DomAttributes":
        remaning_keys = set(node.keys()).difference(
            [
                "children",
                "children_roles_count",
                "nb_pruned_children",
                "group_role",
                "group_roles",
                "markdown",
                "id",
                "path",
                "role",
                "name",
                "level",
                "only_text_roles",
                # Add any other irrelevant keys here
                "orientation",
                "eid",
                "method",
            ]
        )
        return DomAttributes.init(**{key: node[key] for key in remaning_keys})  # type: ignore


@dataclass(frozen=True)
class ComputedDomAttributes:
    in_viewport: bool = False
    is_interactive: bool = False
    is_top_element: bool = False
    is_editable: bool = False
    shadow_root: bool = False
    highlight_index: int | None = None
    selectors: NodeSelectors | None = None

    def set_selectors(self, selectors: NodeSelectors) -> None:
        object.__setattr__(self, "selectors", selectors)


@dataclass(frozen=True)
class DomNode:
    id: str | None
    type: NodeType
    role: NodeRole | str
    text: str
    children: list["DomNode"]
    attributes: DomAttributes | None
    computed_attributes: ComputedDomAttributes
    subtree_ids: list[str] = field(init=False, default_factory=list)
    # parents cannot be set in the constructor because it is a recursive structure
    # we need to set it after the constructor
    parent: "DomNode | None" = None

    def __post_init__(self) -> None:
        subtree_ids: list[str] = [] if self.id is None else [self.id]
        for child in self.children:
            subtree_ids.extend(child.subtree_ids)
        object.__setattr__(self, "subtree_ids", subtree_ids)
        if isinstance(self.role, str):
            object.__setattr__(self, "role", NodeRole.from_value(self.role))

    def set_parent(self, parent: "DomNode | None") -> None:
        object.__setattr__(self, "parent", parent)

    def inner_text(self) -> str:
        if self.type == NodeType.TEXT:
            return self.text
        texts: list[str] = []
        for child in self.children:
            # inner text is not allowed to be hidden
            # or not visible
            # or disabled
            child_text = child.inner_text()
            if len(child_text) == 0:
                continue
            elif child.attributes is None:
                texts.append(child.inner_text())
            elif child.attributes.hidden is not None and not child.attributes.hidden:
                continue
            elif child.attributes.visible is not None and not child.attributes.visible:
                continue
            elif child.attributes.enabled is not None and not child.attributes.enabled:
                continue
            else:
                texts.append(child.inner_text())
        return " ".join(texts)

    @staticmethod
    def from_a11y_node(node: A11yNode, notte_selector: str = "") -> "DomNode":
        children = [DomNode.from_a11y_node(child, notte_selector) for child in node.get("children", [])]
        node_id = node.get("id")
        node_role = NodeRole.from_value(node["role"])
        node_type = NodeType.INTERACTION if node_id is not None else NodeType.OTHER
        if not isinstance(node_role, str) and node_role.category().value == NodeCategory.TEXT.value:
            node_type = NodeType.TEXT
        highlight_index: int | None = node.get("highlight_index")  # type: ignore
        return DomNode(
            id=node_id,
            type=node_type,
            role=node_role,
            text=node["name"],
            children=children,
            attributes=DomAttributes.from_a11y_node(node),
            computed_attributes=ComputedDomAttributes(
                in_viewport=bool(node.get("in_viewport", False)),
                is_interactive=bool(node.get("is_interactive", False)),
                is_top_element=bool(node.get("is_top_element", False)),
                shadow_root=bool(node.get("shadow_root", False)),
                highlight_index=highlight_index,
                # TODO: fix this and compute selectors directly from the a11y node
                selectors=None,
            ),
        )

    def get_role_str(self) -> str:
        if isinstance(self.role, str):
            return self.role
        return self.role.value

    def get_url(self) -> str | None:
        attr = self.computed_attributes.selectors
        if attr is None or len(attr.notte_selector or "") == 0:
            return None
        return attr.notte_selector.split(":")[0]

    def find(self, id: str) -> "InteractionDomNode | None":
        if self.id == id:
            return self.to_interaction_node()
        for child in self.children:
            found = child.find(id)
            if found:
                return found
        return None

    def is_interaction(self) -> bool:
        if isinstance(self.role, str):
            return False
        if self.id is None:
            return False
        if self.type.value == NodeType.INTERACTION.value:
            return True
        return self.role.category().value in [NodeCategory.INTERACTION.value]

    def is_image(self) -> bool:
        if isinstance(self.role, str):
            return False
        if self.id is None:
            return False
        return self.role.category().value == NodeCategory.IMAGE.value

    def flatten(self, only_interaction: bool = False) -> list["DomNode"]:
        def inner(node: DomNode, acc: list["DomNode"]) -> list["DomNode"]:
            if not only_interaction or node.is_interaction():
                acc.append(node)
            for child in node.children:
                _ = inner(child, acc)
            return acc

        return inner(self, [])

    @staticmethod
    def find_all_matching_subtrees_with_parents(
        node: "DomNode", predicate: Callable[["DomNode"], bool]
    ) -> Sequence["DomNode"]:
        """TODO: same implementation for A11yNode and DomNode"""

        if predicate(node):
            return [node]

        matches: list[DomNode] = []
        for child in node.children:
            matching_subtrees = DomNode.find_all_matching_subtrees_with_parents(child, predicate)
            matches.extend(matching_subtrees)

        return matches

    def prune_non_dialogs_if_present(self) -> Sequence["DomNode"]:
        """TODO: make it work with A11yNode and DomNode"""

        def is_dialog(node: DomNode) -> bool:
            return node.role == NodeRole.DIALOG and node.computed_attributes.in_viewport

        dialogs = DomNode.find_all_matching_subtrees_with_parents(self, is_dialog)

        if len(dialogs) == 0:
            # no dialogs found, return node
            return [self]

        return dialogs

    def interaction_nodes(self) -> Sequence["InteractionDomNode"]:
        inodes = self.flatten(only_interaction=True)
        return [inode.to_interaction_node() for inode in inodes]

    def image_nodes(self) -> list["DomNode"]:
        return [node for node in self.flatten() if node.is_image()]

    def subtree_filter(self, ft: Callable[["DomNode"], bool], verbose: bool = False) -> "DomNode | None":
        def inner(node: DomNode) -> DomNode | None:
            children = node.children
            if not ft(node):
                return None

            filtered_children: list[DomNode] = []
            for child in children:
                filtered_child = inner(child)
                if filtered_child is not None:
                    filtered_children.append(filtered_child)
                    # need copy the parent
            if node.id is None and len(filtered_children) == 0 and node.text.strip() == "":
                return None
            return DomNode(
                id=node.id,
                type=node.type,
                role=node.role,
                text=node.text,
                children=filtered_children,
                attributes=node.attributes,
                computed_attributes=node.computed_attributes,
                parent=node.parent,
            )

        start = time.time()
        snode = inner(self)
        end = time.time()
        if verbose:
            logger.info(f"🔍 Filtering subtree of full graph done in {end - start:.2f} seconds")
        return snode

    def subtree_without(self, roles: set[str]) -> "DomNode":

        def only_roles(node: DomNode) -> bool:
            if isinstance(node.role, str):
                return True
            return node.role.value not in roles

        filtered = self.subtree_filter(only_roles)
        if filtered is None:
            raise NodeFilteringResultsInEmptyGraph(
                url=self.get_url(),
                operation=f"subtree_without(roles={roles})",
            )
        return filtered

    def to_interaction_node(self) -> "InteractionDomNode":
        if self.type.value != NodeType.INTERACTION.value:
            raise InvalidInternalCheckError(
                check=(
                    "DomNode must be an interaction node to be converted to an interaction node. "
                    f"But is: {self.type} with id: {self.id}, role: {self.role}, text: {self.text}"
                ),
                url=self.get_url(),
                dev_advice="This should never happen.",
            )
        return InteractionDomNode(
            id=self.id,
            type=NodeType.INTERACTION,
            role=self.role,
            text=self.text,
            attributes=self.attributes,
            computed_attributes=self.computed_attributes,
            # children are not allowed in interaction nodes
            children=[],
            parent=self.parent,
        )


class InteractionDomNode(DomNode):
    id: str  # type: ignore
    type: NodeType = NodeType.INTERACTION

    def __post_init__(self) -> None:
        if self.id is None:
            raise InvalidInternalCheckError(
                check="InteractionNode must have a valid non-None id",
                url=self.get_url(),
                dev_advice=(
                    "This should technically never happen since the id should always be set "
                    "when creating an interaction node."
                ),
            )
        if len(self.children) > 0:
            raise InvalidInternalCheckError(
                check="InteractionNode must have no children",
                url=self.get_url(),
                dev_advice=(
                    "This should technically never happen but you should check the `pruning.py` file "
                    "to diagnose this issue."
                ),
            )
        super().__post_init__()


@dataclass(frozen=True)
class ResolvedLocator:
    role: NodeRole | str
    is_editable: bool
    input_type: str | None
    selector: NodeSelectors
