from typing import final

from loguru import logger
from patchright.async_api import Locator

from notte.actions.base import Action, ActionParameterValue, ExecutableAction
from notte.browser.dom_tree import DomNode, NodeSelectors, ResolvedLocator
from notte.browser.driver import BrowserDriver
from notte.browser.processed_snapshot import ProcessedBrowserSnapshot
from notte.browser.snapshot import BrowserSnapshot
from notte.errors.processing import InvalidInternalCheckError
from notte.errors.resolution import (
    FailedNodeResolutionError,
    FailedUniqueLocatorResolutionError,
)
from notte.pipe.preprocessing.a11y.conflict_resolution import (
    get_html_selector,
    get_locator_for_node_id,
)


@final
class ActionNodeResolutionPipe:

    def __init__(self, browser: BrowserDriver) -> None:
        self._browser = browser

    async def forward(
        self,
        action: Action,
        params_values: list[ActionParameterValue],
        context: ProcessedBrowserSnapshot,
    ) -> ExecutableAction:
        node = context.node.find(action.id)
        if node is None:
            raise InvalidInternalCheckError(
                check=f"Node with id {action.id} not found in graph",
                url=context.snapshot.metadata.url,
                dev_advice=(
                    "ActionNodeResolutionPipe should only be called on nodes that are present in the graph "
                    "or with valid ids."
                ),
            )
        if node.id != action.id:
            raise InvalidInternalCheckError(
                check=f"Resolved node id {node.id} does not match action id {action.id}",
                url=context.snapshot.metadata.url,
                dev_advice=(
                    "ActionNodeResolutionPipe should only be called on nodes that are present in the graph "
                    "or with valid ids."
                ),
            )

        resolved_locator = await self.compute_attributes(node, context.snapshot)
        return ExecutableAction(
            id=action.id,
            description=action.description,
            category=action.category,
            params=action.params,
            params_values=params_values,
            status="valid",
            code=None,
            locator=resolved_locator,
        )

    async def fill_node_selectors(self, node: DomNode, snapshot: BrowserSnapshot) -> None:
        selectors = node.computed_attributes.selectors
        if selectors is None:
            assert node.id is not None
            locator = await get_locator_for_node_id(self._browser.page, snapshot.a11y_tree.raw, node.id)
            if locator is None:
                raise FailedNodeResolutionError(node)
            # You can now use the locator for interaction
            _selectors = await get_html_selector(locator)
            if _selectors is None:
                raise FailedNodeResolutionError(node)
            selectors = NodeSelectors(
                playwright_selector=_selectors.playwright_selector,
                css_selector=_selectors.css_selector,
                xpath_selector=_selectors.xpath_selector,
                notte_selector="",
                in_iframe=False,
                in_shadow_root=False,
                iframe_parent_css_selectors=[],
            )
            node.computed_attributes.set_selectors(selectors)

    async def get_valid_locator(self, selectors: NodeSelectors) -> tuple[Locator, str]:
        if len(selectors.selectors()) == 0:
            raise InvalidInternalCheckError(
                check="No selectors found",
                url="unknown url",
                dev_advice="No selectors found for node",
            )
        for selector in selectors.selectors():
            for frame in self._browser.page.frames:
                try:
                    # Check if selector matches exactly one element
                    locator = frame.locator(selector)
                    count = await locator.count()
                    if count == 1:
                        # Found unique match, perform click
                        return locator, selector
                except Exception as e:
                    logger.error(f"Error with selector '{selector}' on frame '{frame}': {str(e)}, trying next...")
                    continue
        raise FailedUniqueLocatorResolutionError(selectors)

    async def compute_attributes(
        self,
        node: DomNode,
        snapshot: BrowserSnapshot,
    ) -> ResolvedLocator:
        if node.id is None:
            raise InvalidInternalCheckError(
                url=snapshot.metadata.url,
                check="node.id cannot be None",
                dev_advice="ActionNodeResolutionPipe should only be called on nodes with a valid id.",
            )
        selectors = node.computed_attributes.selectors
        if selectors is None:
            await self.fill_node_selectors(node, snapshot)
            selectors = node.computed_attributes.selectors
        if selectors is None:
            raise InvalidInternalCheckError(
                check="No selectors found",
                url=snapshot.metadata.url,
                dev_advice="No selectors found for node",
            )
        # logger.info(f"Selectors filled for node {node.id}: {selectors}")
        locator, selector = await self.get_valid_locator(selectors)
        is_editable = await locator.is_editable(timeout=100)
        input_type = None
        if is_editable:
            input_type = await locator.get_attribute("type", timeout=100)
        visible = await locator.is_visible(timeout=100)
        enabled = await locator.is_enabled(timeout=100)

        if not visible or not enabled:
            raise FailedNodeResolutionError(node)

        return ResolvedLocator(
            role=node.role,
            is_editable=is_editable,
            input_type=input_type,
            selector=NodeSelectors(
                playwright_selector=selector,
                css_selector=selectors.css_selector,
                xpath_selector=selectors.xpath_selector,
                notte_selector=selectors.notte_selector,
                in_iframe=selectors.in_iframe,
                in_shadow_root=selectors.in_shadow_root,
                iframe_parent_css_selectors=selectors.iframe_parent_css_selectors,
            ),
        )
