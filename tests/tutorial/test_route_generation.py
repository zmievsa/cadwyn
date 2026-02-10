from unittest.mock import MagicMock, patch
import pytest
from fastapi.routing import APIRoute
import fastapi.utils
from cadwyn.route_generation import _get_body_model
from cadwyn.schema_generation import _AnnotationTransformer

class TestGetBodyModel:
    @pytest.fixture
    def mock_route_with_simple_body(self):
        route = MagicMock(spec=APIRoute)
        route.body_field = MagicMock()
        # By default assume simple body schema logic returns True
        return route

    def test_get_body_model_legacy_fastapi(self, mock_route_with_simple_body):
        """Simulate FastAPI < 0.128.7 where ModelField.type_ exists"""
        route = mock_route_with_simple_body
        route.body_field.type_ = "LegacyType"
        route.body_field.field_info.annotation = "ModernType"
        
        with patch("cadwyn.route_generation._route_has_a_simple_body_schema", return_value=True):
            assert _get_body_model(route) == "LegacyType"

    def test_get_body_model_modern_fastapi(self, mock_route_with_simple_body):
        """Simulate FastAPI >= 0.128.7 where ModelField.type_ is removed"""
        route = mock_route_with_simple_body
        del route.body_field.type_
        route.body_field.field_info.annotation = "ModernType"
        
        with patch("cadwyn.route_generation._route_has_a_simple_body_schema", return_value=True):
            assert _get_body_model(route) == "ModernType"
            
    def test_get_body_model_with_cadwyn_original_model(self, mock_route_with_simple_body):
        """Verify __cadwyn_original_model__ unwrapping works with both paths"""
        route = mock_route_with_simple_body
        
        # Setup modern path with wrapped model
        del route.body_field.type_
        original_model = "OriginalModel"
        wrapped_model = MagicMock()
        wrapped_model.__cadwyn_original_model__ = original_model
        route.body_field.field_info.annotation = wrapped_model
        
        with patch("cadwyn.route_generation._route_has_a_simple_body_schema", return_value=True):
            assert _get_body_model(route) == original_model

    def test_migrate_route_to_version_legacy_fastapi(self):
        """Simulate FastAPI < 0.128.7 where create_cloned_field exists"""
        mock_create_cloned = MagicMock(return_value="ClonedField")
        
        # Force create_cloned_field availability
        with patch.object(fastapi.utils, "create_cloned_field", mock_create_cloned, create=True):
            transformer = _AnnotationTransformer(MagicMock())
            route = MagicMock(spec=APIRoute)
            route.path = "/path"
            route.response_model = "ResponseModel"
            route.unique_id = "uid"
            # dependencies/endpoint must be mocked to avoid failure in other parts of the method
            route.dependencies = []
            route.endpoint = lambda: None
            route.callbacks = []
            
            transformer.migrate_route_to_version(route)
            
            assert route.secure_cloned_response_field == "ClonedField"
            mock_create_cloned.assert_called_once()

    def test_migrate_route_to_version_modern_fastapi(self):
        """Simulate FastAPI >= 0.128.7 where create_cloned_field is removed"""
        class FakeRoute:
            path = "/path"
            response_model = "ResponseModel"
            unique_id = "uid"
            dependencies = []
            callbacks = []
            response_field = None
            def __init__(self):
                self.endpoint = lambda: None
            
        route = FakeRoute()
        
        # Force create_cloned_field un-availability (None makes getattr return None)
        with patch.object(fastapi.utils, "create_cloned_field", None, create=True):
            transformer = _AnnotationTransformer(MagicMock())
            transformer.migrate_route_to_version(route)
            
            # Verify assignment did NOT happen
            assert not hasattr(route, "secure_cloned_response_field")
