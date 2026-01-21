"""
Tests for the Vision Engine (Level 3) component.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import os
import io

os.environ["OPENAI_API_KEY"] = "test-key-for-testing"

from app.core.levels.vision import VisionEngine, BoundingBox


class TestBoundingBox:
    """Tests for BoundingBox dataclass."""
    
    def test_create_bbox(self):
        """Test creating a bounding box."""
        bbox = BoundingBox(
            x=100,
            y=200,
            width=50,
            height=30,
            mark_id=1,
            role="button",
            name="Submit"
        )
        
        assert bbox.x == 100
        assert bbox.y == 200
        assert bbox.width == 50
        assert bbox.height == 30
        assert bbox.mark_id == 1
    
    def test_center_property(self):
        """Test center calculation."""
        bbox = BoundingBox(
            x=100,
            y=200,
            width=50,
            height=30,
            mark_id=1
        )
        
        center = bbox.center
        
        assert center == (125, 215)
    
    def test_contains_point_inside(self):
        """Test point inside bounding box."""
        bbox = BoundingBox(
            x=100,
            y=100,
            width=100,
            height=100,
            mark_id=1
        )
        
        assert bbox.contains_point(150, 150) is True
        assert bbox.contains_point(100, 100) is True
        assert bbox.contains_point(200, 200) is True
    
    def test_contains_point_outside(self):
        """Test point outside bounding box."""
        bbox = BoundingBox(
            x=100,
            y=100,
            width=100,
            height=100,
            mark_id=1
        )
        
        assert bbox.contains_point(50, 50) is False
        assert bbox.contains_point(250, 150) is False
        assert bbox.contains_point(150, 250) is False


class TestVisionEngine:
    """Tests for VisionEngine class."""
    
    @pytest.fixture
    def mock_openai(self):
        """Create a mock OpenAI client."""
        mock = MagicMock()
        mock.chat = MagicMock()
        mock.chat.completions = MagicMock()
        mock.chat.completions.create = AsyncMock()
        return mock
    
    def test_init(self, mock_openai):
        """Test VisionEngine initialization."""
        vision = VisionEngine(mock_openai, vision_model="gpt-4o")
        
        assert vision._vision_model == "gpt-4o"
        assert vision._page is None
        assert len(vision._bounding_boxes) == 0
    
    @pytest.mark.asyncio
    async def test_attach_detach(self, mock_openai):
        """Test attaching and detaching from page."""
        vision = VisionEngine(mock_openai)
        
        mock_page = MagicMock()
        await vision.attach(mock_page)
        
        assert vision._page == mock_page
        
        vision.detach()
        
        assert vision._page is None
    
    def test_get_mark_by_id(self, mock_openai):
        """Test getting mark by ID."""
        vision = VisionEngine(mock_openai)
        
        bbox1 = BoundingBox(x=0, y=0, width=10, height=10, mark_id=1)
        bbox2 = BoundingBox(x=20, y=20, width=10, height=10, mark_id=2)
        
        vision._bounding_boxes = [bbox1, bbox2]
        
        result = vision.get_mark_by_id(2)
        
        assert result == bbox2
        assert result.mark_id == 2
    
    def test_get_mark_by_id_not_found(self, mock_openai):
        """Test getting non-existent mark."""
        vision = VisionEngine(mock_openai)
        
        bbox1 = BoundingBox(x=0, y=0, width=10, height=10, mark_id=1)
        vision._bounding_boxes = [bbox1]
        
        result = vision.get_mark_by_id(999)
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_take_screenshot_no_page(self, mock_openai):
        """Test taking screenshot without page."""
        vision = VisionEngine(mock_openai)
        
        with pytest.raises(RuntimeError, match="not attached"):
            await vision.take_screenshot()
    
    @pytest.mark.asyncio
    async def test_click_by_coordinates(self, mock_openai):
        """Test clicking by coordinates."""
        vision = VisionEngine(mock_openai)
        
        mock_page = MagicMock()
        mock_page.mouse = MagicMock()
        mock_page.mouse.click = AsyncMock()
        
        await vision.attach(mock_page)
        
        result = await vision.click_by_coordinates(100, 200)
        
        assert result is True
        mock_page.mouse.click.assert_called_once_with(100, 200)


class TestVisionEngineSelectors:
    """Tests for VisionEngine selectors."""
    
    def test_interactive_selectors_defined(self):
        """Test that interactive selectors are defined."""
        assert "button" in VisionEngine.INTERACTIVE_SELECTORS
        assert "a[href]" in VisionEngine.INTERACTIVE_SELECTORS
        assert "input" in VisionEngine.INTERACTIVE_SELECTORS
    
    def test_mark_colors_defined(self):
        """Test that mark colors are defined."""
        assert VisionEngine.MARK_COLOR_BOX == (255, 0, 0)  # Red
        assert VisionEngine.MARK_COLOR_TAG_BG == (255, 255, 255)  # White
        assert VisionEngine.MARK_COLOR_TAG_TEXT == (255, 0, 0)  # Red


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
