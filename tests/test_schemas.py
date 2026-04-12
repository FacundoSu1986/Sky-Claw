# -*- coding: utf-8 -*-
"""
test_schemas.py - Tests unitarios completos para schemas de validación Pydantic.

Este módulo contiene tests unitarios para verificar el funcionamiento correcto
de los esquemas Pydantic y los validadores de seguridad del sistema Sky_Claw.
"""
import pytest
from datetime import datetime
from pydantic import ValidationError

from sky_claw.core.schemas import (
    ModMetadata,
    ScrapingQuery,
    SecurityAuditRequest,
    SecurityAuditResponse,
    AgentToolRequest,
    AgentToolResponse,
)


# =============================================================================
# Tests Unitarios para ModMetadata
# =============================================================================

class TestModMetadata:
    """Tests para el schema ModMetadata."""
    
    def test_mod_metadata_valid(self):
        """Test creación válida de ModMetadata."""
        mod = ModMetadata(
            mod_id=12345,
            name="Test Armor Mod",
            version="1.0.0",
            category="armor",
            author="TestAuthor",
            dependencies=[1, 2, 3],
            description="A test mod description"
        )
        assert mod.mod_id == 12345
        assert mod.name == "Test Armor Mod"
        assert mod.version == "1.0.0"
        assert mod.category == "armor"
        assert mod.author == "TestAuthor"
        assert mod.dependencies == [1, 2, 3]
        assert mod.description == "A test mod description"
        assert isinstance(mod.downloaded_at, datetime)
    
    def test_mod_metadata_invalid_extra_fields(self):
        """Test que campos extra son rechazados (extra='forbid')."""
        with pytest.raises(ValidationError) as exc_info:
            ModMetadata(
                mod_id=1,
                name="Test",
                version="1.0.0",
                category="other",
                author="Author",
                malicious_field="this should fail"  # Campo extra no permitido
            )
        assert "Extra inputs are not permitted" in str(exc_info.value)
    
    def test_mod_metadata_sanitize_name(self):
        """Test sanitización de caracteres peligrosos en name."""
        # Test con caracteres HTML/script
        mod = ModMetadata(
            mod_id=1,
            name='<script>alert("xss")</script>Mod Name',
            version="1.0.0",
            category="other",
            author="Author"
        )
        assert "<script>" not in mod.name
        assert "</script>" not in mod.name
        assert '"' not in mod.name
        
        # Test con saltos de línea
        mod2 = ModMetadata(
            mod_id=2,
            name="Mod\nWith\rNewlines",
            version="1.0.0",
            category="other",
            author="Author"
        )
        assert "\n" not in mod2.name
        assert "\r" not in mod2.name
    
    def test_mod_metadata_invalid_version(self):
        """Test versión inválida (no coincide patrón semver)."""
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id=1,
                name="Test",
                version="invalid",
                category="other",
                author="Author"
            )
    
    def test_mod_metadata_invalid_version_v_prefix(self):
        """Test versión con prefijo 'v' es inválida."""
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id=1,
                name="Test",
                version="v1.0.0",
                category="other",
                author="Author"
            )
    
    def test_mod_metadata_invalid_mod_id_zero(self):
        """Test mod_id inválido (cero)."""
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id=0,
                name="Test",
                version="1.0.0",
                category="other",
                author="Author"
            )
    
    def test_mod_metadata_invalid_mod_id_negative(self):
        """Test mod_id inválido (negativo)."""
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id=-1,
                name="Test",
                version="1.0.0",
                category="other",
                author="Author"
            )
    
    def test_mod_metadata_invalid_category(self):
        """Test categoría inválida."""
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id=1,
                name="Test",
                version="1.0.0",
                category="invalid_category",
                author="Author"
            )
    
    def test_mod_metadata_valid_categories(self):
        """Test todas las categorías válidas."""
        valid_categories = ["armor", "weapon", "quest", "interface", "gameplay", "other"]
        for category in valid_categories:
            mod = ModMetadata(
                mod_id=1,
                name="Test",
                version="1.0.0",
                category=category,
                author="Author"
            )
            assert mod.category == category
    
    def test_mod_metadata_name_max_length(self):
        """Test longitud máxima de name (200 caracteres)."""
        long_name = "A" * 200
        mod = ModMetadata(
            mod_id=1,
            name=long_name,
            version="1.0.0",
            category="other",
            author="Author"
        )
        assert mod.name == long_name
        
        # Exceder longitud máxima
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id=1,
                name="A" * 201,
                version="1.0.0",
                category="other",
                author="Author"
            )
    
    def test_mod_metadata_author_max_length(self):
        """Test longitud máxima de author (100 caracteres)."""
        long_author = "A" * 100
        mod = ModMetadata(
            mod_id=1,
            name="Test",
            version="1.0.0",
            category="other",
            author=long_author
        )
        assert mod.author == long_author
        
        # Exceder longitud máxima
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id=1,
                name="Test",
                version="1.0.0",
                category="other",
                author="A" * 101
            )


# =============================================================================
# Tests Unitarios para ScrapingQuery
# =============================================================================

class TestScrapingQuery:
    """Tests para el schema ScrapingQuery."""
    
    def test_scraping_query_valid(self):
        """Test creación válida de ScrapingQuery."""
        query = ScrapingQuery(query="test mod search")
        assert query.query == "test mod search"
        assert query.url is None
        assert query.mod_id is None
        assert query.force_stealth is False
        assert query.target_data is None
        assert query.include_description is True
    
    def test_scraping_query_with_target_data(self):
        """Test ScrapingQuery con campo target_data."""
        for target in ["dependencies", "files", "changelog", "forum_known_issues"]:
            query = ScrapingQuery(query="test", target_data=target)
            assert query.target_data == target
    
    def test_scraping_query_sanitize_query(self):
        """Test sanitización de consultas."""
        query = ScrapingQuery(query='<script>alert("xss")</script>search')
        assert "<script>" not in query.query
        assert "</script>" not in query.query
        assert '"' not in query.query
        assert "'" not in query.query
    
    def test_scraping_query_invalid_target_data(self):
        """Test target_data inválido."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", target_data="invalid_target")
    
    def test_scraping_query_with_mod_id(self):
        """Test ScrapingQuery con mod_id válido."""
        query = ScrapingQuery(query="test", mod_id=12345)
        assert query.mod_id == 12345
    
    def test_scraping_query_invalid_mod_id_zero(self):
        """Test mod_id inválido (cero)."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", mod_id=0)
    
    def test_scraping_query_invalid_mod_id_negative(self):
        """Test mod_id inválido (negativo)."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", mod_id=-1)
    
    def test_scraping_query_force_stealth(self):
        """Test opción force_stealth."""
        query = ScrapingQuery(query="test", force_stealth=True)
        assert query.force_stealth is True
    
    def test_scraping_query_include_description_false(self):
        """Test opción include_description en False."""
        query = ScrapingQuery(query="test", include_description=False)
        assert query.include_description is False
    
    def test_scraping_query_extra_fields_forbidden(self):
        """Test que campos extra son rechazados."""
        with pytest.raises(ValidationError) as exc_info:
            ScrapingQuery(query="test", extra_field="not allowed")
        assert "Extra inputs are not permitted" in str(exc_info.value)
    
    def test_scraping_query_empty_query(self):
        """Test query vacío es inválido."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="")
    
    def test_scraping_query_max_length(self):
        """Test longitud máxima de query (500 caracteres)."""
        long_query = "A" * 500
        query = ScrapingQuery(query=long_query)
        assert query.query == long_query
        
        with pytest.raises(ValidationError):
            ScrapingQuery(query="A" * 501)


# =============================================================================
# Tests Unitarios para SecurityAuditRequest
# =============================================================================

class TestSecurityAuditRequest:
    """Tests para el schema SecurityAuditRequest."""
    
    def test_security_audit_request_valid(self):
        """Test creación válida de SecurityAuditRequest."""
        request = SecurityAuditRequest(
            target_path="mods/test.esp",
            audit_type="file"
        )
        assert request.target_path == "mods/test.esp"
        assert request.audit_type == "file"
        assert request.depth == 1
        assert request.include_vectors is True
    
    def test_security_audit_request_with_options(self):
        """Test SecurityAuditRequest con opciones completas."""
        request = SecurityAuditRequest(
            target_path="mods/directory",
            audit_type="directory",
            depth=3,
            include_vectors=False
        )
        assert request.target_path == "mods/directory"
        assert request.audit_type == "directory"
        assert request.depth == 3
        assert request.include_vectors is False
    
    def test_security_audit_request_valid_audit_types(self):
        """Test todos los audit_type válidos."""
        for audit_type in ["file", "repository", "directory"]:
            request = SecurityAuditRequest(
                target_path="test",
                audit_type=audit_type
            )
            assert request.audit_type == audit_type
    
    def test_security_audit_request_invalid_audit_type(self):
        """Test audit_type inválido."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(
                target_path="test",
                audit_type="invalid_type"
            )
    
    def test_security_audit_request_depth_bounds(self):
        """Test límites de depth (1-5)."""
        # Valores válidos
        for depth in [1, 3, 5]:
            request = SecurityAuditRequest(target_path="test", depth=depth)
            assert request.depth == depth
        
        # Valores inválidos
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="test", depth=0)
        
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="test", depth=6)
    
    def test_security_audit_request_empty_path(self):
        """Test path vacío es inválido."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="")
    
    def test_security_audit_request_extra_fields_forbidden(self):
        """Test que campos extra son rechazados."""
        with pytest.raises(ValidationError) as exc_info:
            SecurityAuditRequest(target_path="test", extra_field="not allowed")
        assert "Extra inputs are not permitted" in str(exc_info.value)


# =============================================================================
# Tests Unitarios para SecurityAuditResponse
# =============================================================================

class TestSecurityAuditResponse:
    """Tests para el schema SecurityAuditResponse."""
    
    def test_security_audit_response_valid(self):
        """Test creación válida de SecurityAuditResponse."""
        response = SecurityAuditResponse(
            target="mods/test.esp",
            findings=[{"type": "warning", "message": "Test finding"}],
            risk_score=0.5,
            recommendations=["Update mod"]
        )
        assert response.target == "mods/test.esp"
        assert len(response.findings) == 1
        assert response.risk_score == 0.5
        assert len(response.recommendations) == 1
        assert isinstance(response.audited_at, datetime)
    
    def test_security_audit_response_with_multiple_findings(self):
        """Test respuesta con múltiples findings."""
        response = SecurityAuditResponse(
            target="test",
            findings=[
                {"type": "warning", "message": "Warning 1"},
                {"type": "error", "message": "Error 1"},
                {"type": "info", "message": "Info 1"}
            ],
            risk_score=0.8,
            recommendations=["Fix 1", "Fix 2", "Fix 3"]
        )
        assert len(response.findings) == 3
        assert len(response.recommendations) == 3
    
    def test_security_audit_response_risk_score_bounds(self):
        """Test límites de risk_score (0.0 - 1.0)."""
        # Valores válidos
        for score in [0.0, 0.5, 1.0]:
            response = SecurityAuditResponse(
                target="test",
                findings=[],
                risk_score=score,
                recommendations=[]
            )
            assert response.risk_score == score
        
        # Valor mayor a 1.0
        with pytest.raises(ValidationError):
            SecurityAuditResponse(
                target="test",
                findings=[],
                risk_score=1.5,
                recommendations=[]
            )
        
        # Valor negativo
        with pytest.raises(ValidationError):
            SecurityAuditResponse(
                target="test",
                findings=[],
                risk_score=-0.1,
                recommendations=[]
            )
    
    def test_security_audit_response_empty_findings(self):
        """Test respuesta con lista de findings vacía."""
        response = SecurityAuditResponse(
            target="test",
            findings=[],
            risk_score=0.0,
            recommendations=[]
        )
        assert response.findings == []
        assert response.recommendations == []
    
    def test_security_audit_response_extra_fields_forbidden(self):
        """Test que campos extra son rechazados."""
        with pytest.raises(ValidationError) as exc_info:
            SecurityAuditResponse(
                target="test",
                findings=[],
                risk_score=0.5,
                recommendations=[],
                extra_field="not allowed"
            )
        assert "Extra inputs are not permitted" in str(exc_info.value)


# =============================================================================
# Tests Negativos - Validador Anti-SSRF
# =============================================================================

class TestSSRFValidation:
    """Tests para validación Anti-SSRF en ScrapingQuery."""
    
    def test_ssrf_blocks_localhost(self):
        """Test que URLs con localhost son bloqueadas."""
        with pytest.raises(ValidationError) as exc_info:
            ScrapingQuery(query="test", url="http://localhost/admin")
        assert "SSRF" in str(exc_info.value) or "bloqueado" in str(exc_info.value).lower()
    
    def test_ssrf_blocks_127_0_0_1(self):
        """Test que URLs con 127.0.0.1 son bloqueadas."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://127.0.0.1/admin")
    
    def test_ssrf_blocks_aws_metadata(self):
        """Test que AWS metadata endpoint es bloqueado."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://169.254.169.254/latest/meta-data/")
    
    def test_ssrf_blocks_private_ip_10(self):
        """Test que IPs privadas 10.x.x.x son bloqueadas."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://10.0.0.1/internal")
        
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://10.255.255.255/secret")
    
    def test_ssrf_blocks_private_ip_192_168(self):
        """Test que IPs privadas 192.168.x.x son bloqueadas."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://192.168.1.1/admin")
        
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://192.168.0.100/config")
    
    def test_ssrf_blocks_private_ip_172_16(self):
        """Test que IPs privadas 172.16.x.x son bloqueadas."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://172.16.0.1/internal")
        
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://172.31.255.255/secret")
    
    def test_ssrf_blocks_localhost_localdomain(self):
        """Test que localhost.localdomain es bloqueado."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://localhost.localdomain/admin")
    
    def test_ssrf_blocks_google_metadata(self):
        """Test que Google Cloud metadata endpoint es bloqueado."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://metadata.google.internal/computeMetadata/v1/")
    
    def test_ssrf_blocks_kubernetes_internal(self):
        """Test que endpoints internos de Kubernetes son bloqueados."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://kubernetes.default/api/v1/namespaces")
    
    def test_ssrf_blocks_internal_domain(self):
        """Test que dominios .internal son bloqueados."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://internal.service.internal/data")
    
    def test_ssrf_blocks_local_domain(self):
        """Test que dominios .local son bloqueados."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="http://service.local/data")
    
    def test_ssrf_blocks_invalid_scheme(self):
        """Test que schemes no HTTP/HTTPS son bloqueados."""
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="file:///etc/passwd")
        
        with pytest.raises(ValidationError):
            ScrapingQuery(query="test", url="ftp://internal.server/file")
    
    def test_ssrf_accepts_public_url(self):
        """Test que URLs públicas son aceptadas."""
        # Esta URL debe ser aceptada sin lanzar ValidationError
        query = ScrapingQuery(
            query="test",
            url="https://www.nexusmods.com/skyrimspecialedition/mods/1234"
        )
        assert query.url is not None
        assert "nexusmods.com" in query.url
    
    def test_ssrf_accepts_http_public_url(self):
        """Test que URLs HTTP públicas son aceptadas."""
        query = ScrapingQuery(
            query="test",
            url="http://example.com/page"
        )
        assert query.url is not None


# =============================================================================
# Tests Negativos - Validador Anti-Path Traversal
# =============================================================================

class TestPathTraversalValidation:
    """Tests para validación Anti-Path Traversal en SecurityAuditRequest."""
    
    def test_path_traversal_blocks_double_dot_slash(self):
        """Test que ../ es bloqueado."""
        with pytest.raises(ValidationError) as exc_info:
            SecurityAuditRequest(target_path="../../../etc/passwd")
        assert "traversal" in str(exc_info.value).lower() or "path" in str(exc_info.value).lower()
    
    def test_path_traversal_blocks_double_dot_backslash(self):
        """Test que ..\\ es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="..\\..\\windows\\system32")
    
    def test_path_traversal_blocks_null_byte(self):
        """Test que bytes nulos son bloqueados."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="file.txt%00.jpg")
    
    def test_path_traversal_blocks_null_byte_hex(self):
        """Test que bytes nulos hex son bloqueados."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="file.txt\x00.jpg")
    
    def test_path_traversal_blocks_url_encoded_slash(self):
        """Test que ..%2f (URL encoded /) es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="..%2f..%2fetc%2fpasswd")
    
    def test_path_traversal_blocks_url_encoded_backslash(self):
        """Test que ..%5c (URL encoded \\) es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="..%5c..%5cetc%5cpasswd")
    
    def test_path_traversal_blocks_absolute_unix_path(self):
        """Test que rutas absolutas Unix son bloqueadas."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="/etc/passwd")
        
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="/root/.ssh/id_rsa")
    
    def test_path_traversal_blocks_absolute_windows_path(self):
        """Test que rutas absolutas Windows son bloqueadas."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="C:\\Windows\\System32")
        
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="D:\\secret\\data")
    
    def test_path_traversal_blocks_unc_path(self):
        """Test que rutas UNC son bloqueadas."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="\\\\server\\share\\file")
    
    def test_path_traversal_accepts_valid_path(self):
        """Test que rutas válidas son aceptadas."""
        # Rutas relativas válidas
        request = SecurityAuditRequest(target_path="mods/skyrim/mod.esp")
        assert request.target_path is not None
        
        request2 = SecurityAuditRequest(target_path="data/files/config.json")
        assert request2.target_path is not None
    
    def test_path_traversal_accepts_simple_filename(self):
        """Test que nombres de archivo simples son aceptados."""
        request = SecurityAuditRequest(target_path="mod.esp")
        assert request.target_path == "mod.esp"

    def test_path_traversal_blocks_url_encoded_dots(self):
        """Test que %2e%2e (URL encoded ..) es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="%2e%2e/etc/passwd")

        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="%2e%2e%2fetc%2fpasswd")

    def test_path_traversal_blocks_half_encoded_dots(self):
        """Test que variantes semi-codificadas de .. son bloqueadas."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="%2e./etc/passwd")

        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path=".%2e/etc/passwd")

    def test_path_traversal_blocks_double_url_encoded(self):
        """Test que doble URL encoding (%252e) es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="%252e%252e%252fetc%252fpasswd")

    def test_path_traversal_blocks_overlong_utf8_separator(self):
        """Test que separadores overlong UTF-8 son bloqueados."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="..%c0%af")

        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="..%c1%9c")

    def test_path_traversal_blocks_bare_double_dot(self):
        """Test que .. solo (sin separador) es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="..")

    def test_path_traversal_blocks_dot_dot_at_end(self):
        """Test que foo/.. (.. al final del path) es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="foo/..")

    def test_path_traversal_blocks_encoded_absolute_unix(self):
        """Test que /etc/passwd URL-encoded es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="%2fetc%2fpasswd")

    def test_path_traversal_blocks_encoded_windows_drive(self):
        """Test que drive letter Windows URL-encoded es bloqueado."""
        with pytest.raises(ValidationError):
            SecurityAuditRequest(target_path="%43%3a%5cWindows")


# =============================================================================
# Tests para AgentToolRequest y AgentToolResponse
# =============================================================================

class TestAgentToolRequest:
    """Tests para el schema AgentToolRequest."""
    
    def test_valid_request(self):
        """Test solicitud válida."""
        request = AgentToolRequest(
            tool_name="scrape_mod",
            parameters={"mod_id": 123},
            priority="high"
        )
        assert request.tool_name == "scrape_mod"
        assert request.parameters == {"mod_id": 123}
        assert request.priority == "high"
        assert request.requires_confirmation is False
        assert request.timeout_seconds == 30
    
    def test_invalid_priority(self):
        """Test prioridad inválida."""
        with pytest.raises(ValidationError):
            AgentToolRequest(
                tool_name="test",
                priority="invalid"
            )
    
    def test_valid_priorities(self):
        """Test todas las prioridades válidas."""
        for priority in ["low", "medium", "high", "critical"]:
            request = AgentToolRequest(tool_name="test", priority=priority)
            assert request.priority == priority
    
    def test_requires_confirmation(self):
        """Test opción requires_confirmation."""
        request = AgentToolRequest(tool_name="test", requires_confirmation=True)
        assert request.requires_confirmation is True
    
    def test_timeout_seconds_validation(self):
        """Test validación de timeout_seconds."""
        # Valor válido
        request = AgentToolRequest(tool_name="test", timeout_seconds=60)
        assert request.timeout_seconds == 60
        
        # Valor inválido (cero o negativo)
        with pytest.raises(ValidationError):
            AgentToolRequest(tool_name="test", timeout_seconds=0)
        
        with pytest.raises(ValidationError):
            AgentToolRequest(tool_name="test", timeout_seconds=-1)
    
    def test_empty_tool_name(self):
        """Test tool_name vacío es inválido."""
        with pytest.raises(ValidationError):
            AgentToolRequest(tool_name="")
    
    def test_extra_fields_forbidden(self):
        """Test que campos extra son rechazados."""
        with pytest.raises(ValidationError) as exc_info:
            AgentToolRequest(tool_name="test", extra_field="not allowed")
        assert "Extra inputs are not permitted" in str(exc_info.value)


class TestAgentToolResponse:
    """Tests para el schema AgentToolResponse."""
    
    def test_success_response(self):
        """Test respuesta exitosa."""
        response = AgentToolResponse(
            tool_name="scrape_mod",
            success=True,
            result={"data": "test"}
        )
        assert response.success is True
        assert response.result == {"data": "test"}
        assert response.error is None
    
    def test_error_response(self):
        """Test respuesta con error."""
        response = AgentToolResponse(
            tool_name="scrape_mod",
            success=False,
            error="Connection timeout"
        )
        assert response.success is False
        assert response.result is None
        assert response.error == "Connection timeout"
    
    def test_response_with_execution_time(self):
        """Test respuesta con tiempo de ejecución."""
        response = AgentToolResponse(
            tool_name="test",
            success=True,
            execution_time_ms=150.5
        )
        assert response.execution_time_ms == 150.5
    
    def test_extra_fields_forbidden(self):
        """Test que campos extra son rechazados."""
        with pytest.raises(ValidationError) as exc_info:
            AgentToolResponse(
                tool_name="test",
                success=True,
                extra_field="not allowed"
            )
        assert "Extra inputs are not permitted" in str(exc_info.value)


# =============================================================================
# Tests de Integración de Agentes (Marcadores)
# =============================================================================

class TestAgentIntegration:
    """
    Tests de integración para verificar que los agentes aceptan
    los schemas Pydantic correctamente.
    
    Nota: Estos tests requieren que los agentes estén configurados
    correctamente en el entorno de testing.
    """
    
    @pytest.mark.asyncio
    async def test_purple_security_agent_accepts_pydantic_request(self):
        """
        Test que purple_security_agent acepta SecurityAuditRequest.
        
        Este test verifica que el agente de seguridad puede recibir
        y procesar un SecurityAuditRequest válido.
        """
        # Crear request válido
        request = SecurityAuditRequest(
            target_path="mods/test.esp",
            audit_type="file",
            depth=1
        )
        
        # Verificar que el request se construye correctamente
        assert request.target_path == "mods/test.esp"
        assert request.audit_type == "file"
        
        # Nota: La integración real con el agente requeriría
        # importar y instanciar el agente, lo cual puede requerir
        # configuración adicional del entorno.
        # Este test sirve como contrato de interfaz.
    
    @pytest.mark.asyncio
    async def test_scraper_agent_accepts_scraping_query(self):
        """
        Test que scraperAgent acepta ScrapingQuery.
        
        Este test verifica que el agente de scraping puede recibir
        y procesar un ScrapingQuery válido.
        """
        # Crear query válida
        query = ScrapingQuery(
            query="skyrim armor mods",
            target_data="files",
            include_description=True
        )
        
        # Verificar que el query se construye correctamente
        assert query.query == "skyrim armor mods"
        assert query.target_data == "files"
        assert query.include_description is True
        
        # Nota: La integración real con el agente requeriría
        # importar y instanciar el agente.
    
    @pytest.mark.asyncio
    async def test_mod_metadata_serialization_for_agent(self):
        """
        Test que ModMetadata puede serializarse correctamente
        para envío entre agentes.
        """
        mod = ModMetadata(
            mod_id=12345,
            name="Test Mod",
            version="1.0.0",
            category="armor",
            author="TestAuthor",
            dependencies=[1, 2, 3]
        )
        
        # Verificar serialización a dict
        mod_dict = mod.model_dump()
        assert mod_dict["mod_id"] == 12345
        assert mod_dict["name"] == "Test Mod"
        
        # Verificar serialización a JSON
        mod_json = mod.model_dump_json()
        assert '"mod_id":12345' in mod_json
        assert '"name":"Test Mod"' in mod_json


# =============================================================================
# Tests Adicionales de Validación Estricta
# =============================================================================

class TestStrictValidation:
    """Tests para verificar validación estricta (strict=True)."""
    
    def test_mod_metadata_strict_type_validation(self):
        """Test que tipos estrictos son validados."""
        # Pasar string donde se espera int debe fallar
        with pytest.raises(ValidationError):
            ModMetadata(
                mod_id="12345",  # String en lugar de int
                name="Test",
                version="1.0.0",
                category="other",
                author="Author"
            )
    
    def test_scraping_query_strict_type_validation(self):
        """Test que tipos estrictos son validados en ScrapingQuery."""
        # Pasar int donde se espera str debe fallar
        with pytest.raises(ValidationError):
            ScrapingQuery(
                query=12345,  # Int en lugar de string
                force_stealth="true"  # String en lugar de bool
            )
    
    def test_security_audit_request_strict_type_validation(self):
        """Test que tipos estrictos son validados en SecurityAuditRequest."""
        # Pasar string donde se espera int
        with pytest.raises(ValidationError):
            SecurityAuditRequest(
                target_path="test",
                depth="3"  # String en lugar de int
            )
