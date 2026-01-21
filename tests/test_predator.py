"""
Tests for the PredatorBrowser main orchestrator.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import os

# Set a dummy API key for tests
os.environ["OPENAI_API_KEY"] = "test-key-for-testing"

from app.core.predator import (
    PredatorBrowser,
    BrowserConfig,
    ExecutionResult,
    WaterfallLevel
)


class TestBrowserConfig:
    """Tests for BrowserConfig."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = BrowserConfig()
        
        assert config.headless is True
        assert config.viewport_width == 1920
        assert config.viewport_height == 1080
        assert config.stealth_mode is True
        assert config.router_model == "gpt-4o-mini"
        assert config.vision_model == "gpt-4o"
    
    def test_custom_config(self):
        """Test custom configuration values."""
        config = BrowserConfig(
            headless=False,
            viewport_width=1280,
            viewport_height=720,
            stealth_mode=False,
            router_model="gpt-4-turbo"
        )
        
        assert config.headless is False
        assert config.viewport_width == 1280
        assert config.viewport_height == 720
        assert config.stealth_mode is False
        assert config.router_model == "gpt-4-turbo"


class TestExecutionResult:
    """Tests for ExecutionResult."""
    
    def test_successful_result(self):
        """Test successful execution result."""
        result = ExecutionResult(
            success=True,
            level=WaterfallLevel.L1_SHADOW_API,
            data={"price": 99.99},
            action_taken="Extracted from network traffic",
            execution_time_ms=45.0
        )
        
        assert result.success is True
        assert result.level == WaterfallLevel.L1_SHADOW_API
        assert result.data == {"price": 99.99}
        assert result.error is None
    
    def test_failed_result(self):
        """Test failed execution result."""
        result = ExecutionResult(
            success=False,
            level=WaterfallLevel.FAILED,
            error="Could not find element",
            execution_time_ms=1000.0
        )
        
        assert result.success is False
        assert result.level == WaterfallLevel.FAILED
        assert result.error == "Could not find element"
    
    def test_to_dict(self):
        """Test conversion to dictionary."""
        result = ExecutionResult(
            success=True,
            level=WaterfallLevel.L2_AX_TREE,
            data={"title": "Test"},
            action_taken="Clicked button",
            execution_time_ms=100.0,
            url_changed=True,
            dom_changed=True
        )
        
        d = result.to_dict()
        
        assert d["success"] is True
        assert d["level"] == "L2_AX_TREE"
        assert d["data"] == {"title": "Test"}
        assert d["url_changed"] is True
        assert d["dom_changed"] is True


class TestWaterfallLevel:
    """Tests for WaterfallLevel enum."""
    
    def test_level_values(self):
        """Test enum values."""
        assert WaterfallLevel.L1_SHADOW_API.value == "L1_SHADOW_API"
        assert WaterfallLevel.L2_AX_TREE.value == "L2_AX_TREE"
        assert WaterfallLevel.L3_VISION.value == "L3_VISION"
        assert WaterfallLevel.FAILED.value == "FAILED"


class TestPredatorBrowserInit:
    """Tests for PredatorBrowser initialization."""
    
    def test_init_with_config(self):
        """Test initialization with custom config."""
        config = BrowserConfig(headless=True)
        browser = PredatorBrowser(config)
        
        assert browser.config == config
        assert browser._initialized is False
    
    def test_init_without_api_key(self):
        """Test initialization fails without API key."""
        # Remove the test API key
        original_key = os.environ.get("OPENAI_API_KEY")
        del os.environ["OPENAI_API_KEY"]
        
        try:
            with pytest.raises(ValueError, match="OpenAI API key required"):
                config = BrowserConfig(openai_api_key=None)
                PredatorBrowser(config)
        finally:
            # Restore the key
            if original_key:
                os.environ["OPENAI_API_KEY"] = original_key
            else:
                os.environ["OPENAI_API_KEY"] = "test-key-for-testing"


@pytest.mark.asyncio
class TestPredatorBrowserAsync:
    """Async tests for PredatorBrowser."""
    
    async def test_session_context_manager(self):
        """Test the session context manager."""
        config = BrowserConfig(openai_api_key="test-key")
        browser = PredatorBrowser(config)
        
        # Mock the initialize and close methods
        browser.initialize = AsyncMock()
        browser.close = AsyncMock()
        
        async with browser.session() as b:
            assert b == browser
            browser.initialize.assert_called_once()
        
        browser.close.assert_called_once()


# Integration tests would go here with actual browser instances
# These require a running Playwright browser and would be marked with
# @pytest.mark.integration


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
