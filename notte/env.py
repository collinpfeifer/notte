import asyncio
import datetime as dt
from collections.abc import Sequence
from typing import Unpack

from loguru import logger
from pydantic import BaseModel

from notte.actions.base import (
    Action,
    ActionParameter,
    ActionParameterValue,
    BrowserAction,
)
from notte.browser.dom_tree import InteractionDomNode
from notte.browser.driver import BrowserConfig, BrowserDriver
from notte.browser.node_type import NodeRole
from notte.browser.observation import Observation, TrajectoryProgress
from notte.browser.pool import BrowserPool
from notte.browser.processed_snapshot import ProcessedBrowserSnapshot
from notte.browser.snapshot import BrowserSnapshot
from notte.common.logging import timeit
from notte.common.resource import AsyncResource
from notte.controller.actions import (
    BaseAction,
    BrowserActionId,
    GotoAction,
    InteractionAction,
    SelectDropdownOptionAction,
    WaitAction,
)
from notte.controller.base import BrowserController
from notte.controller.proxy import NotteActionProxy
from notte.errors.actions import InvalidActionError
from notte.errors.env import MaxStepsReachedError, NoContextObservedError
from notte.errors.processing import InvalidInternalCheckError
from notte.llms.service import LLMService
from notte.pipe.action.base import BaseActionSpacePipe
from notte.pipe.action.pipe import (
    ActionSpaceType,
    MainActionSpaceConfig,
    MainActionSpacePipe,
)
from notte.pipe.preprocessing.dom.locate import selectors_through_shadow_dom
from notte.pipe.preprocessing.pipe import PreprocessingType, ProcessedSnapshotPipe
from notte.pipe.resolution import ActionNodeResolutionPipe
from notte.pipe.scraping.config import ScrapingConfig, ScrapingType
from notte.pipe.scraping.pipe import DataScrapingPipe
from notte.sdk.types import (
    DEFAULT_MAX_NB_STEPS,
    PaginationObserveRequestDict,
    PaginationParams,
    ScrapeParams,
)


class SimpleActionResolutionPipe:

    @staticmethod
    def forward(action: BaseAction, context: ProcessedBrowserSnapshot | None = None) -> BaseAction:
        if not isinstance(action, InteractionAction) or context is None:
            # no need to resolve
            return action
        if isinstance(action, SelectDropdownOptionAction):
            return SimpleActionResolutionPipe.resolve_selector_locators(action, context)
        selector_map: dict[str, InteractionDomNode] = {inode.id: inode for inode in context.interaction_nodes()}
        if action.id not in selector_map:
            raise InvalidActionError(action_id=action.id, reason=f"action '{action.id}' not found in page context.")
        node = selector_map[action.id]
        if node.computed_attributes.selectors is None:
            raise InvalidInternalCheckError(
                check=f"No selector found for action {action.id}",
                url=None,
                dev_advice=(
                    (
                        "This technnically should never happen. There is likely an issue during playright "
                        "conflict resolution pipeline, i.e `notte.pipe.preprocessing.a11y.conflict_resolution.py`."
                    )
                ),
            )
        selectors = node.computed_attributes.selectors
        if selectors.in_shadow_root:
            logger.info(f"🔍 Resolving shadow root selectors for {node.id} ({node.text})")
            selectors = selectors_through_shadow_dom(node)
        action.selector = selectors
        action.text_label = node.text
        return action

    @staticmethod
    def resolve_selector_locators(
        action: SelectDropdownOptionAction,
        context: ProcessedBrowserSnapshot,
    ) -> SelectDropdownOptionAction:
        """
        Resolve the selector locators for a dropdown option.

        We need to find the selector node and the option node.
        This function simply iterates over the interaction nodes to find the option node.
        The selector node is the first node with a role in [COMBOBOX, LISTBOX, LIST]
        that appears before the option node.
        """
        inodes = context.node.interaction_nodes()
        snode = None
        for node in inodes:
            if node.get_role_str() in [NodeRole.COMBOBOX.value, NodeRole.LISTBOX.value, NodeRole.LIST.value]:
                snode = node
            if (action.option_id is not None and node.id == action.option_id) or (
                action.value is not None and node.text == action.value and node.get_role_str() == NodeRole.OPTION.value
            ):
                if snode is None:
                    raise ValueError(f"No select html element found for {action.option_id} or {action.value}")

                if node.computed_attributes.selectors is None or snode.computed_attributes.selectors is None:
                    raise InvalidInternalCheckError(
                        check=f"Cannot find associated selector element for option node {node.id}",
                        url=None,
                        dev_advice=(
                            (
                                "This technnically should never happen. There is likely an issue during playright "
                                "conflict resolution pipe, i.e `SimpleActionResolutionPipe`."
                            )
                        ),
                    )
                selectors = snode.computed_attributes.selectors
                option_selectors = node.computed_attributes.selectors
                logger.info(
                    (
                        f"Resolved locators for select dropdown {snode.id} ({snode.text})"
                        f" and option {node.id} ({node.text})"
                    )
                )
                action.option_selector = option_selectors
                action.selector = selectors
                return action
        raise InvalidInternalCheckError(
            check=f"No select html element found for {action.option_id} or {action.value}",
            url=None,
            dev_advice=(
                (
                    "This technnically should never happen. There is likely an issue during playright "
                    "conflict resolution pipeline, i.e `notte.pipe.preprocessing.a11y.conflict_resolution.py`."
                )
            ),
        )


class NotteEnvConfig(BaseModel):
    max_steps: int = DEFAULT_MAX_NB_STEPS
    processing_type: PreprocessingType = PreprocessingType.A11Y
    browser: BrowserConfig = BrowserConfig()
    scraping: ScrapingConfig = ScrapingConfig()
    action: MainActionSpaceConfig = MainActionSpaceConfig()
    observe_max_retry_after_snapshot_update: int = 2
    nb_seconds_between_snapshots_check: int = 10
    auto_scrape: bool = True

    @staticmethod
    def llm_tagging() -> "NotteEnvConfig":
        return NotteEnvConfig()

    @staticmethod
    def simple() -> "NotteEnvConfig":
        return NotteEnvConfig(
            max_steps=100,
            auto_scrape=False,
            processing_type=PreprocessingType.DOM,
            scraping=ScrapingConfig(type=ScrapingType.SIMPLE),
            action=MainActionSpaceConfig(type=ActionSpaceType.SIMPLE),
        )


class TrajectoryStep(BaseModel):
    obs: Observation
    action: BaseAction


class NotteEnv(AsyncResource):
    def __init__(
        self,
        config: NotteEnvConfig | None = None,
        headless: bool = False,
        browser: BrowserDriver | None = None,
        pool: BrowserPool | None = None,
        llmserve: LLMService | None = None,
    ) -> None:
        if config is not None:
            logger.info(f"🔧 Custom notte-env config: \n{config.model_dump_json(indent=2)}")
        if llmserve is None:
            llmserve = LLMService()
        self.config: NotteEnvConfig = config or NotteEnvConfig.llm_tagging()
        self.config.browser.headless = headless
        self._browser: BrowserDriver = browser or BrowserDriver(pool=pool, config=self.config.browser)
        super().__init__(self._browser)
        self.controller: BrowserController = BrowserController(self._browser)

        self.trajectory: list[TrajectoryStep] = []
        self._context: ProcessedBrowserSnapshot | None = None
        self._action_space_pipe: BaseActionSpacePipe = MainActionSpacePipe(llmserve=llmserve, config=self.config.action)
        self._data_scraping_pipe: DataScrapingPipe = DataScrapingPipe(llmserve=llmserve, browser=self._browser)

    @property
    def context(self) -> ProcessedBrowserSnapshot:
        if self._context is None:
            raise NoContextObservedError()
        return self._context

    @property
    def previous_actions(self) -> Sequence[BaseAction] | None:
        # This function is always called after trajectory.append(preobs)
        # —This means trajectory[-1] is always the "current (pre)observation"
        # And trajectory[-2] is the "previous observation" we're interested in.
        if len(self.trajectory) <= 1:
            return None
        previous_obs: Observation = self.trajectory[-2].obs
        if not previous_obs.has_space():
            return None  # we don't have a space for pre-observations
        if self.obs.clean_url != previous_obs.clean_url:
            return None  # the page has significantly changed
        if previous_obs.space is None:
            raise InvalidInternalCheckError(
                check="Previous observation has no space. This should never happen.",
                url=previous_obs.metadata.url,
                dev_advice=(
                    "This technnically should never happen. There is likely an issue during the action space pipe."
                ),
            )
        return previous_obs.space.actions("all")

    @property
    def obs(self) -> Observation:
        if len(self.trajectory) <= 0:
            raise NoContextObservedError()
        return self.trajectory[-1].obs

    def progress(self) -> TrajectoryProgress:
        return TrajectoryProgress(
            max_steps=self.config.max_steps,
            current_step=len(self.trajectory),
        )

    # ---------------------------- observe, step functions ----------------------------

    def _preobserve(self, snapshot: BrowserSnapshot, action: BaseAction) -> Observation:
        if len(self.trajectory) >= self.config.max_steps:
            raise MaxStepsReachedError(max_steps=self.config.max_steps)
        self._context = ProcessedSnapshotPipe.forward(snapshot, type=self.config.processing_type)
        preobs = Observation.from_snapshot(snapshot, progress=self.progress())
        self.trajectory.append(TrajectoryStep(obs=preobs, action=action))
        return preobs

    async def _observe(
        self,
        pagination: PaginationParams,
        retry: int,
    ) -> Observation:
        logger.info(f"🔍 observing page {self.context.snapshot.metadata.url}")
        self.obs.space = self._action_space_pipe.forward(
            self.context,
            self.previous_actions,
            pagination=pagination,
        )
        # TODO: improve this
        # Check if the snapshot has changed since the beginning of the trajectory
        # if it has, it means that the page was not fully loaded and that we should restart the oblisting
        time_diff = dt.datetime.now() - self.context.snapshot.metadata.timestamp
        if time_diff.total_seconds() > self.config.nb_seconds_between_snapshots_check:
            logger.warning(
                (
                    f"{time_diff.total_seconds()} seconds since the beginning of the action listing."
                    f"Check if page content has changed..."
                )
            )
            check_snapshot = await self._browser.snapshot(screenshot=False)
            if not self.context.snapshot.compare_with(check_snapshot) and retry > 0:
                logger.warning("Snapshot changed since the beginning of the action listing, retrying to observe again")
                _ = self._preobserve(check_snapshot, action=WaitAction(time_ms=int(time_diff.total_seconds() * 1000)))
                return await self._observe(retry=retry - 1, pagination=pagination)

        if (
            self.config.auto_scrape
            and self.obs.space.category is not None
            and self.obs.space.category.is_data()
            and not self.obs.has_data()
        ):
            self.obs.data = await self._data_scraping_pipe.forward(self.context, self.config.scraping)
        return self.obs

    @timeit("goto")
    async def goto(self, url: str | None) -> Observation:
        snapshot = await self._browser.goto(url)
        return self._preobserve(snapshot, action=GotoAction(url=snapshot.metadata.url))

    @timeit("observe")
    async def observe(
        self,
        url: str | None = None,
        **pagination: Unpack[PaginationObserveRequestDict],
    ) -> Observation:
        _ = await self.goto(url)
        logger.debug(f"ℹ️ previous actions IDs: {[a.id for a in self.previous_actions or []]}")
        logger.debug(f"ℹ️ context inodes IDs: {[node.id for node in self.context.interaction_nodes()]}")
        return await self._observe(
            pagination=PaginationParams.model_validate(pagination),
            retry=self.config.observe_max_retry_after_snapshot_update,
        )

    @timeit("execute")
    async def execute(
        self,
        action_id: str,
        params: dict[str, str] | str | None = None,
        enter: bool | None = None,
    ) -> Observation:
        if not BrowserAction.is_special(action_id):
            # Scrape action is a special case
            if action_id == BrowserActionId.SCRAPE:
                return await self.scrape()
        elif action_id not in [inode.id for inode in self.context.interaction_nodes()]:
            raise InvalidActionError(action_id=action_id, reason=f"action '{action_id}' not found in page context.")
        action, _params = self._parse_env(action_id, params)

        enter = enter if enter is not None else action.id.startswith("I")
        exec_action = await ActionNodeResolutionPipe(self._browser).forward(action, _params, self.context)
        browser_action = NotteActionProxy.forward(exec_action, enter=enter)
        snapshot = await self.controller.execute(browser_action)
        obs = self._preobserve(snapshot, action=browser_action)
        return obs

    async def raw_step(
        self,
        action: BaseAction,
    ) -> Observation:
        logger.info(f"🌌 starting execution of action {action.id}...")
        if BrowserAction.is_special(action.id):
            # Scrape action is a special case
            if action.id == BrowserActionId.SCRAPE.value:
                # TODO: we do scraping and observation in one step
                return await self.god()
        action = SimpleActionResolutionPipe.forward(action, self._context)
        snapshot = await self.controller.execute(action)
        logger.info(f"🌌 action {action.id} executed in browser. Observing page...")
        _ = self._preobserve(snapshot, action=action)
        return await self._observe(
            pagination=PaginationParams(),
            retry=self.config.observe_max_retry_after_snapshot_update,
        )

    @timeit("step")
    async def step(
        self,
        action_id: str,
        params: dict[str, str] | str | None = None,
        enter: bool | None = None,
        **pagination: Unpack[PaginationObserveRequestDict],
    ) -> Observation:
        _ = await self.execute(action_id, params, enter=enter)
        logger.debug(f"ℹ️ previous actions IDs: {[a.id for a in self.previous_actions or []]}")
        logger.debug(f"ℹ️ context inodes IDs: {[node.id for node in self.context.interaction_nodes()]}")
        return await self._observe(
            pagination=PaginationParams.model_validate(pagination),
            retry=self.config.observe_max_retry_after_snapshot_update,
        )

    @timeit("scrape")
    async def scrape(
        self,
        url: str | None = None,
        only_main_content: bool = True,
        scrape_images: bool = False,
    ) -> Observation:
        if url is not None:
            _ = await self.goto(url)
        self.config.scraping.params = ScrapeParams(
            only_main_content=only_main_content,
            scrape_images=scrape_images,
        )
        self.obs.data = await self._data_scraping_pipe.forward(
            self.context,
            self.config.scraping,
        )
        return self.obs

    @timeit("god")
    async def god(self, url: str | None = None, **pagination: Unpack[PaginationObserveRequestDict]) -> Observation:
        logger.info("🌊 God mode activated (scraping + action listing)")
        if url is not None:
            _ = await self.goto(url)
        _pagination = PaginationParams.model_validate(pagination)
        space, data = await asyncio.gather(
            self._action_space_pipe.forward_async(self.context, self.previous_actions, pagination=_pagination),
            self._data_scraping_pipe.forward_async(self.context, self.config.scraping),
        )
        self.obs.space = space
        self.obs.data = data
        return self.obs

    @timeit("reset")
    async def reset(self) -> None:
        self.trajectory = []
        self._context = None
        return await self._browser.reset()

    # ------------------------------ Private ---------------------------------------

    def _parse_env(
        self, action_id: str, params: dict[str, str] | str | None = None
    ) -> tuple[Action, list[ActionParameterValue]]:
        if isinstance(params, str):
            params = {"value": params}
        _param_values: list[ActionParameterValue] = []
        _params: list[ActionParameter] = []
        if params is not None:
            _param_values = [
                ActionParameterValue(
                    parameter_name=name,
                    value=value,
                )
                for name, value in params.items()
            ]
            _params = [
                ActionParameter(
                    name=name,
                    type="string",
                )
                for name in params.keys()
            ]
        return (
            Action(
                id=action_id,
                description="ID only",
                category="",
                status="valid",
                params=_params,
            ),
            _param_values,
        )
