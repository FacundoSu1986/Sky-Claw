# -*- coding: utf-8 -*-
"""
test_lcel_stability.py - Pruebas de estabilidad para la integración LCEL.
Verifica que la lógica de importación condicional sea robusta y que no existan
errores de dependencias residuales.
"""

import sys
import os
import traceback

# Insertar el directorio del proyecto al inicio del path para evitar conflictos
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def test_core_imports():
    """Test 1: Core module imports"""
    print("=" * 60)
    print("Test 1: Core Module Imports")
    print("=" * 60)
    try:
        from sky_claw.core import (  # noqa: F401
            CircuitBreakerTripped,
            WSLInteropError,
            DatabaseAgent,
            ModMetadata,
            ScrapingQuery,
            SecurityAuditRequest,
            SecurityAuditResponse,
            AgentToolRequest,
            AgentToolResponse,
            RouteClassification,
            validate_input,
            validate_output,
            validate_contract,
            get_contract_schema,
        )

        print("[PASS] All core module imports successful")
        return True
    except ImportError as e:
        print(f"[FAIL] Core import error: {e}")
        traceback.print_exc()
        return False


def test_agent_imports():
    """Test 2: Agent module imports"""
    print()
    print("=" * 60)
    print("Test 2: Agent Module Imports")
    print("=" * 60)
    try:
        from sky_claw.agent import (  # noqa: F401
            AsyncToolRegistry,
            LLMRouter,
            ToolExecutor,
            PromptComposer,
            ChainBuilder,
            RouteClassification,
        )

        print("[PASS] All agent module imports successful")
        return True
    except ImportError as e:
        print(f"[FAIL] Agent import error: {e}")
        traceback.print_exc()
        return False


def test_lcel_conditional_import():
    """Test 3: LCEL conditional import"""
    print()
    print("=" * 60)
    print("Test 3: LCEL Conditional Import")
    print("=" * 60)
    try:
        from sky_claw.agent.lcel_chains import LANGCHAIN_AVAILABLE

        print(f"[INFO] LANGCHAIN_AVAILABLE = {LANGCHAIN_AVAILABLE}")
        if not LANGCHAIN_AVAILABLE:
            print("[PASS] Graceful degradation: LangChain not installed, using stubs")
        else:
            print("[PASS] LangChain is available")
        return True
    except ImportError as e:
        print(f"[FAIL] LCEL import error: {e}")
        traceback.print_exc()
        return False


def test_route_classification():
    """Test 4: RouteClassification validation"""
    print()
    print("=" * 60)
    print("Test 4: RouteClassification Validation")
    print("=" * 60)
    from sky_claw.core.schemas import RouteClassification

    # Test 4a: Valid classification
    try:
        route = RouteClassification(
            intent="CONSULTA_MODDING",
            confidence=0.85,
            target_agent="SupervisorAgent",
            requires_context=True,
        )
        print(
            f"[PASS] Valid route created: intent={route.intent}, confidence={route.confidence}"
        )
    except Exception as e:
        print(f"[FAIL] Valid route error: {e}")
        return False

    # Test 4b: Invalid confidence (out of range)
    try:
        route = RouteClassification(
            intent="CHAT_GENERAL",
            confidence=1.5,  # Invalid: > 1.0
        )
        print("[FAIL] Should have failed for invalid confidence")
        return False
    except Exception as e:
        print(f"[PASS] Correctly rejected invalid confidence: {type(e).__name__}")

    # Test 4c: Invalid intent
    try:
        route = RouteClassification(
            intent="INVALID_INTENT",  # Invalid intent
            confidence=0.5,
        )
        print("[FAIL] Should have failed for invalid intent")
        return False
    except Exception as e:
        print(f"[PASS] Correctly rejected invalid intent: {type(e).__name__}")

    return True


def test_tool_executor():
    """Test 5: ToolExecutor functionality"""
    print()
    print("=" * 60)
    print("Test 5: ToolExecutor Functionality")
    print("=" * 60)
    try:
        from sky_claw.agent.lcel_chains import ToolExecutor

        executor = ToolExecutor(
            tool_name="test_tool", tool_description="Test description"
        )
        result = executor({"param": "value"})
        print(f"[PASS] ToolExecutor executed successfully: {result[:50]}...")
        return True
    except Exception as e:
        print(f"[FAIL] ToolExecutor error: {e}")
        traceback.print_exc()
        return False


def test_prompt_composer():
    """Test 6: PromptComposer functionality"""
    print()
    print("=" * 60)
    print("Test 6: PromptComposer Functionality")
    print("=" * 60)
    try:
        from sky_claw.agent.lcel_chains import PromptComposer

        composer = PromptComposer(system_prompt="Test system prompt")

        # Test tool prompt
        prompt = composer.compose_tool_prompt(
            tool_name="test_tool",
            tool_input={"query": "test"},
            tool_description="Test description",
        )
        print(f"[PASS] Tool prompt composed with {len(prompt)} messages")

        # Test RAG prompt
        rag_prompt = composer.compose_rag_prompt(
            query="test query", context="test context", sources=["source1", "source2"]
        )
        print(f"[PASS] RAG prompt composed with {len(rag_prompt)} messages")

        return True
    except Exception as e:
        print(f"[FAIL] PromptComposer error: {e}")
        traceback.print_exc()
        return False


def test_chain_builder():
    """Test 7: ChainBuilder functionality"""
    print()
    print("=" * 60)
    print("Test 7: ChainBuilder Functionality")
    print("=" * 60)
    try:
        from sky_claw.agent.lcel_chains import ChainBuilder, ToolExecutor

        executor = ToolExecutor(tool_name="test", tool_description="Test")
        builder = ChainBuilder(tool_executor=executor)

        # Test tool chain creation
        chain = builder.create_tool_chain("test_tool", "Test description")
        print(f"[PASS] Tool chain created, type: {type(chain).__name__}")

        # Test sequential chain
        steps = [
            {"tool": "step1", "description": "First step"},
            {"tool": "step2", "description": "Second step"},
        ]
        seq_chain = builder.create_sequential_chain(steps, "Test task")
        print(f"[PASS] Sequential chain created, type: {type(seq_chain).__name__}")

        return True
    except Exception as e:
        print(f"[FAIL] ChainBuilder error: {e}")
        traceback.print_exc()
        return False


def main():
    """Run all stability tests"""
    print()
    print("*" * 60)
    print("SKY_CLAW LCEL STABILITY TEST SUITE")
    print("*" * 60)
    print(f"Python version: {sys.version}")
    print()

    results = []
    results.append(("Core Imports", test_core_imports()))
    results.append(("Agent Imports", test_agent_imports()))
    results.append(("LCEL Conditional Import", test_lcel_conditional_import()))
    results.append(("RouteClassification", test_route_classification()))
    results.append(("ToolExecutor", test_tool_executor()))
    results.append(("PromptComposer", test_prompt_composer()))
    results.append(("ChainBuilder", test_chain_builder()))

    print()
    print("*" * 60)
    print("TEST RESULTS SUMMARY")
    print("*" * 60)
    passed = 0
    failed = 0
    for name, result in results:
        status = "[PASS]" if result else "[FAIL]"
        print(f"{status} {name}")
        if result:
            passed += 1
        else:
            failed += 1

    print()
    print(f"Total: {passed} passed, {failed} failed")

    if failed == 0:
        print()
        print("=" * 60)
        print("ALL STABILITY TESTS PASSED")
        print("=" * 60)
        return 0
    else:
        print()
        print("=" * 60)
        print("SOME TESTS FAILED - REVIEW REQUIRED")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
