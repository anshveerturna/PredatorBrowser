"""
Tests for the Sniffer (Level 1) component.
"""

import pytest
import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from collections import deque
import os

os.environ["OPENAI_API_KEY"] = "test-key-for-testing"

from app.core.levels.sniffer import Sniffer, NetworkCapture


class TestNetworkCapture:
    """Tests for NetworkCapture dataclass."""
    
    def test_create_capture(self):
        """Test creating a network capture."""
        capture = NetworkCapture(
            url="https://api.example.com/data",
            status=200,
            content_type="application/json",
            body={"items": [{"id": 1, "name": "Test"}]}
        )
        
        assert capture.url == "https://api.example.com/data"
        assert capture.status == 200
        assert capture.body == {"items": [{"id": 1, "name": "Test"}]}
    
    def test_get_structure_simple(self):
        """Test extracting structure from simple JSON."""
        capture = NetworkCapture(
            url="https://api.example.com/data",
            status=200,
            content_type="application/json",
            body={"name": "John", "age": 30, "active": True}
        )
        
        structure = capture.get_structure()
        
        assert "name" in structure
        assert "age" in structure
        assert "active" in structure
    
    def test_get_structure_nested(self):
        """Test extracting structure from nested JSON."""
        capture = NetworkCapture(
            url="https://api.example.com/data",
            status=200,
            content_type="application/json",
            body={
                "user": {
                    "profile": {
                        "name": "John"
                    }
                },
                "items": [{"id": 1}]
            }
        )
        
        structure = capture.get_structure(max_depth=3)
        
        assert "user" in structure
        assert "items" in structure
    
    def test_get_structure_with_array(self):
        """Test extracting structure from JSON with arrays."""
        capture = NetworkCapture(
            url="https://api.example.com/data",
            status=200,
            content_type="application/json",
            body={"items": [{"id": 1}, {"id": 2}]}
        )
        
        structure = capture.get_structure()
        
        assert "items" in structure
        assert isinstance(structure["items"], list)


class TestSniffer:
    """Tests for Sniffer class."""
    
    @pytest.fixture
    def mock_openai(self):
        """Create a mock OpenAI client."""
        mock = MagicMock()
        mock.chat = MagicMock()
        mock.chat.completions = MagicMock()
        mock.chat.completions.create = AsyncMock()
        return mock
    
    def test_init(self, mock_openai):
        """Test Sniffer initialization."""
        sniffer = Sniffer(mock_openai, buffer_size=100)
        
        assert sniffer._router_model == "gpt-4o-mini"
        assert sniffer._active is False
        assert len(sniffer._buffer) == 0
    
    def test_clear_buffer(self, mock_openai):
        """Test clearing the buffer."""
        sniffer = Sniffer(mock_openai)
        
        # Add some captures
        sniffer._buffer.append(NetworkCapture(
            url="test", status=200, content_type="application/json", body={}
        ))
        
        assert len(sniffer._buffer) == 1
        
        sniffer.clear_buffer()
        
        assert len(sniffer._buffer) == 0
    
    def test_get_buffer_summary(self, mock_openai):
        """Test getting buffer summary."""
        sniffer = Sniffer(mock_openai)
        
        sniffer._buffer.append(NetworkCapture(
            url="https://api.example.com/data",
            status=200,
            content_type="application/json",
            body={"test": "data"}
        ))
        
        summary = sniffer.get_buffer_summary()
        
        assert len(summary) == 1
        assert summary[0]["url"] == "https://api.example.com/data"
        assert summary[0]["status"] == 200
    
    @pytest.mark.asyncio
    async def test_analyze_traffic_empty_buffer(self, mock_openai):
        """Test analyzing empty buffer returns None."""
        sniffer = Sniffer(mock_openai)
        
        result = await sniffer.analyze_traffic("Find the price")
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_analyze_traffic_with_data(self, mock_openai):
        """Test analyzing traffic with matching data."""
        # Setup mock response
        mock_openai.chat.completions.create = AsyncMock(side_effect=[
            # First call: identify relevant captures
            MagicMock(choices=[MagicMock(message=MagicMock(
                content='{"relevant_indices": [0], "reasoning": "Found price data"}'
            ))]),
            # Second call: extract data
            MagicMock(choices=[MagicMock(message=MagicMock(
                content='{"found": true, "data": {"price": 99.99}, "source_url": "https://api.example.com"}'
            ))])
        ])
        
        sniffer = Sniffer(mock_openai)
        sniffer._buffer.append(NetworkCapture(
            url="https://api.example.com/products",
            status=200,
            content_type="application/json",
            body={"products": [{"name": "Widget", "price": 99.99}]}
        ))
        
        result = await sniffer.analyze_traffic("Find the price")
        
        assert result is not None
        assert result["level"] == "L1_SHADOW_API"
        assert result["data"]["price"] == 99.99


@pytest.mark.asyncio
class TestSnifferSearch:
    """Tests for Sniffer search functionality."""
    
    @pytest.fixture
    def mock_openai(self):
        mock = MagicMock()
        mock.chat = MagicMock()
        mock.chat.completions = MagicMock()
        mock.chat.completions.create = AsyncMock()
        return mock
    
    async def test_search_for_pattern_in_url(self, mock_openai):
        """Test searching for pattern in URL."""
        sniffer = Sniffer(mock_openai)
        
        sniffer._buffer.append(NetworkCapture(
            url="https://api.example.com/products",
            status=200,
            content_type="application/json",
            body={}
        ))
        sniffer._buffer.append(NetworkCapture(
            url="https://api.example.com/users",
            status=200,
            content_type="application/json",
            body={}
        ))
        
        matches = await sniffer.search_for_pattern("products")
        
        assert len(matches) == 1
        assert "products" in matches[0].url
    
    async def test_search_for_pattern_in_body(self, mock_openai):
        """Test searching for pattern in body."""
        sniffer = Sniffer(mock_openai)
        
        sniffer._buffer.append(NetworkCapture(
            url="https://api.example.com/data",
            status=200,
            content_type="application/json",
            body={"secret_key": "12345"}
        ))
        
        matches = await sniffer.search_for_pattern("secret_key")
        
        assert len(matches) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
