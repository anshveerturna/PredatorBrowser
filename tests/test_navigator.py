"""
Tests for the Navigator (Level 2) component.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
import os

os.environ["OPENAI_API_KEY"] = "test-key-for-testing"

from app.core.levels.navigator import Navigator, AXNode


class TestAXNode:
    """Tests for AXNode dataclass."""
    
    def test_create_node(self):
        """Test creating an AX node."""
        node = AXNode(
            role="button",
            name="Submit",
            node_id=1
        )
        
        assert node.role == "button"
        assert node.name == "Submit"
        assert node.node_id == 1
    
    def test_to_markdown_simple(self):
        """Test converting simple node to markdown."""
        node = AXNode(
            role="button",
            name="Click Me",
            node_id=1
        )
        
        markdown = node.to_markdown()
        
        assert "[button]" in markdown
        assert '"Click Me"' in markdown
        assert "(ID: 1)" in markdown
    
    def test_to_markdown_with_attributes(self):
        """Test converting node with attributes to markdown."""
        node = AXNode(
            role="checkbox",
            name="Accept Terms",
            node_id=5,
            checked=True,
            disabled=False
        )
        
        markdown = node.to_markdown()
        
        assert "[checkbox]" in markdown
        assert "checked=True" in markdown
    
    def test_to_markdown_with_children(self):
        """Test converting node with children to markdown."""
        child = AXNode(role="text", name="Hello", node_id=2)
        parent = AXNode(
            role="button",
            name="Greet",
            node_id=1,
            children=[child]
        )
        
        markdown = parent.to_markdown()
        
        assert "[button]" in markdown
        assert "[text]" in markdown
        assert "Hello" in markdown


class TestNavigator:
    """Tests for Navigator class."""
    
    @pytest.fixture
    def mock_openai(self):
        """Create a mock OpenAI client."""
        mock = MagicMock()
        mock.chat = MagicMock()
        mock.chat.completions = MagicMock()
        mock.chat.completions.create = AsyncMock()
        return mock
    
    def test_init(self, mock_openai):
        """Test Navigator initialization."""
        navigator = Navigator(mock_openai, model="gpt-4")
        
        assert navigator._model == "gpt-4"
        assert navigator._page is None
        assert navigator._node_counter == 0
    
    @pytest.mark.asyncio
    async def test_attach_detach(self, mock_openai):
        """Test attaching and detaching from page."""
        navigator = Navigator(mock_openai)
        
        mock_page = MagicMock()
        await navigator.attach(mock_page)
        
        assert navigator._page == mock_page
        
        navigator.detach()
        
        assert navigator._page is None
    
    def test_convert_snapshot(self, mock_openai):
        """Test converting Playwright snapshot to AXNode."""
        navigator = Navigator(mock_openai)
        
        snapshot = {
            "role": "button",
            "name": "Test Button",
            "children": [
                {"role": "text", "name": "Click"}
            ]
        }
        
        node = navigator._convert_snapshot(snapshot)
        
        assert node.role == "button"
        assert node.name == "Test Button"
        assert len(node.children) == 1
        assert node.children[0].role == "text"
    
    @pytest.mark.asyncio
    async def test_build_selector_with_role_and_name(self, mock_openai):
        """Test building selector with role and name."""
        navigator = Navigator(mock_openai)
        
        node_info = {"role": "button", "name": "Submit"}
        
        selector = await navigator._build_selector(node_info)
        
        assert selector == 'role=button[name="Submit"]'
    
    @pytest.mark.asyncio
    async def test_build_selector_with_name_only(self, mock_openai):
        """Test building selector with name only."""
        navigator = Navigator(mock_openai)
        
        node_info = {"name": "Click here"}
        
        selector = await navigator._build_selector(node_info)
        
        assert selector == 'text="Click here"'
    
    @pytest.mark.asyncio
    async def test_get_ax_tree_no_page(self, mock_openai):
        """Test getting AX tree without attached page."""
        navigator = Navigator(mock_openai)
        
        result = await navigator.get_ax_tree()
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_find_element_no_page(self, mock_openai):
        """Test finding element without attached page."""
        navigator = Navigator(mock_openai)
        
        result = await navigator.find_element_by_ax("Click button")
        
        assert result is None


class TestNavigatorInteractiveRoles:
    """Tests for Navigator role detection."""
    
    def test_interactive_roles_defined(self):
        """Test that interactive roles are defined."""
        assert "button" in Navigator.INTERACTIVE_ROLES
        assert "link" in Navigator.INTERACTIVE_ROLES
        assert "textbox" in Navigator.INTERACTIVE_ROLES
        assert "checkbox" in Navigator.INTERACTIVE_ROLES
    
    def test_content_roles_defined(self):
        """Test that content roles are defined."""
        assert "heading" in Navigator.CONTENT_ROLES
        assert "paragraph" in Navigator.CONTENT_ROLES
        assert "text" in Navigator.CONTENT_ROLES


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
