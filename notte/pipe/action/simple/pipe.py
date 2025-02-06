from collections.abc import Sequence

from pydantic import BaseModel
from typing_extensions import override

from notte.actions.base import ActionParameterValue, ExecutableAction
from notte.browser.dom_tree import DomNode, InteractionDomNode, ResolvedLocator
from notte.browser.processed_snapshot import ProcessedBrowserSnapshot
from notte.controller.actions import BaseAction
from notte.controller.space import ActionSpace
from notte.errors.processing import InvalidInternalCheckError
from notte.pipe.action.base import BaseActionSpacePipe
from notte.pipe.rendering.interaction_only import InteractionOnlyDomNodeRenderingPipe
from notte.pipe.rendering.pipe import (
    DomNodeRenderingConfig,
    DomNodeRenderingPipe,
    DomNodeRenderingType,
)
from notte.sdk.types import PaginationParams


class SimpleActionSpaceConfig(BaseModel):
    rendering: DomNodeRenderingConfig = DomNodeRenderingConfig(
        type=DomNodeRenderingType.INTERACTION_ONLY,
        include_images=True,
    )


class SimpleActionSpacePipe(BaseActionSpacePipe):
    def __init__(self, config: SimpleActionSpaceConfig) -> None:
        self.config: SimpleActionSpaceConfig = config

    def node_to_executable(self, node: InteractionDomNode) -> ExecutableAction:
        selectors = node.computed_attributes.selectors
        if selectors is None:
            raise InvalidInternalCheckError(
                check="Node should have an xpath selector",
                url=node.get_url(),
                dev_advice="This should never happen.",
            )
        return ExecutableAction(
            id=node.id,
            category="Interaction action",
            description=InteractionOnlyDomNodeRenderingPipe.render_node(node, self.config.rendering.include_attributes),
            locator=ResolvedLocator(
                selector=selectors,
                is_editable=False,
                input_type=None,
                role=node.role,
            ),
            params_values=[
                ActionParameterValue(
                    parameter_name="value",
                    value="<sample_value>",
                )
            ],
        )

    def actions(self, node: DomNode) -> list[BaseAction]:
        return [self.node_to_executable(inode) for inode in node.interaction_nodes()]

    @override
    def forward(
        self,
        context: ProcessedBrowserSnapshot,
        previous_action_list: Sequence[BaseAction] | None,
        pagination: PaginationParams,
    ) -> ActionSpace:
        page_content = DomNodeRenderingPipe.forward(context.snapshot.dom_node, config=self.config.rendering)
        return ActionSpace(
            description=page_content,
            raw_actions=self.actions(context.snapshot.dom_node),
        )
