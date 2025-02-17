from enum import Enum
from typing import Annotated, Any, Generic, TypeVar

import requests
from pydantic import BaseModel, Field, RootModel, model_serializer, model_validator

from notte.errors.processing import InvalidInternalCheckError

TBaseModel = TypeVar("TBaseModel", bound=BaseModel, covariant=True)
DictBaseModel = RootModel[dict[str, Any] | list[dict[str, Any]]]


class NoStructuredData(BaseModel):
    """Placeholder model for when no structured data is present."""

    pass


class ImageCategory(Enum):
    ICON = "icon"
    CONTENT_IMAGE = "content_image"
    DECORATIVE = "decorative"
    SVG_ICON = "svg_icon"
    SVG_CONTENT = "svg_content"


class ImageData(BaseModel):
    id: Annotated[str, Field(description="Unique identifier for the image")]
    url: Annotated[str | None, Field(description="URL of the image")] = None
    category: Annotated[ImageCategory | None, Field(description="Category of the image (icon, svg, content, etc.)")] = (
        None
    )

    def bytes(self) -> bytes:
        if self.url is None:
            raise InvalidInternalCheckError(
                check="image URL is not available. Cannot retrieve image bytes.",
                url=self.url,
                dev_advice=(
                    "Check the `ImageData` construction process in the `DataScraping` pipeline to diagnose this issue."
                ),
            )
        return requests.get(self.url).content


class StructuredData(BaseModel, Generic[TBaseModel]):
    success: Annotated[bool, Field(description="Whether the data was extracted successfully")] = True
    error: Annotated[str | None, Field(description="Error message if the data was not extracted successfully")] = None
    data: Annotated[
        TBaseModel | DictBaseModel | None, Field(description="Structured data extracted from the page in JSON format")
    ] = None

    @model_validator(mode="before")
    def wrap_dict_in_root_model(cls, values):
        if isinstance(values, dict) and "data" in values and isinstance(values["data"], (dict, list)):
            values["data"] = DictBaseModel(values["data"])
        # if error and is not empty, set success to False
        error = values.get("error")
        if error is not None and len(error.strip()) > 0:
            values["success"] = False
        return values

    @model_serializer
    def serialize_model(self, **kwargs):
        result = {
            "success": self.success,
            "error": self.error,
        }
        if isinstance(self.data, RootModel):
            result["data"] = self.data.root
        elif isinstance(self.data, BaseModel):
            result["data"] = self.data.model_dump()
        else:
            result["data"] = self.data
        return result


class DataSpace(BaseModel):
    markdown: Annotated[str | None, Field(description="Markdown representation of the extracted data")] = None
    images: Annotated[
        list[ImageData] | None, Field(description="List of images extracted from the page (ID and download link)")
    ] = None
    structured: Annotated[
        StructuredData[BaseModel] | None, Field(description="Structured data extracted from the page in JSON format")
    ] = None
