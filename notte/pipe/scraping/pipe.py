from typing import final

from loguru import logger

from notte.browser.driver import BrowserDriver
from notte.browser.processed_snapshot import ProcessedBrowserSnapshot
from notte.data.space import DataSpace
from notte.llms.service import LLMService
from notte.pipe.scraping.complex import ComplexScrapingPipe
from notte.pipe.scraping.config import ScrapingConfig, ScrapingType
from notte.pipe.scraping.simple import SimpleScrapingPipe


@final
class DataScrapingPipe:
    """
    Data scraping pipe that scrapes data from the page
    """

    def __init__(
        self,
        llmserve: LLMService,
        browser: BrowserDriver,
    ) -> None:
        self.complex_pipe = ComplexScrapingPipe(llmserve=llmserve, browser=browser)

    async def forward(
        self,
        context: ProcessedBrowserSnapshot,
        config: ScrapingConfig,
    ) -> DataSpace:
        match config.type:
            case ScrapingType.SIMPLE:
                logger.info("📀 Scraping page with simple scraping pipe")
                data = await SimpleScrapingPipe.forward(context, config)
            case ScrapingType.COMPLEX:
                logger.info("📀 Scraping page with complex/LLM-based scraping pipe")
                data = await self.complex_pipe.forward(context, config)
        logger.info(f"📄 Extracted page as markdown\n: {data.markdown}\n")
        return data

    async def forward_async(self, context: ProcessedBrowserSnapshot, config: ScrapingConfig) -> DataSpace:
        return await self.complex_pipe.forward(context, config)
