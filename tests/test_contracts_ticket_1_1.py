"""Test script for contracts.py refactoring (Ticket 1.1 — C-03)."""

import asyncio
import sys


def main():
    # Test 1: SchemaRegistry population
    from sky_claw.core.contracts import get_schema_class, list_registered_schemas

    schemas = list_registered_schemas()
    assert len(schemas) == 7, f"Expected 7 schemas, got {len(schemas)}"
    print(
        f"[TEST 1 PASS] SchemaRegistry: {len(schemas)} models -> {list(schemas.keys())}"
    )

    # Test 2: O(1) lookup
    atr = get_schema_class("AgentToolRequest")
    assert atr is not None, "AgentToolRequest not found"
    assert atr.__name__ == "AgentToolRequest"
    none_result = get_schema_class("NonExistent")
    assert none_result is None
    print(
        f"[TEST 2 PASS] O(1) lookup: AgentToolRequest={atr.__name__}, NonExistent={none_result}"
    )

    # Test 3: validate_input with valid data
    from sky_claw.core.contracts import validate_input

    class SupervisorAgent:
        @validate_input("dispatch_tool")
        async def dispatch_tool(self, **kwargs):
            return kwargs

    async def test_valid_input():
        agent = SupervisorAgent()
        result = await agent.dispatch_tool(tool_name="test_tool", priority="high")
        assert result["tool_name"] == "test_tool"
        assert result["priority"] == "high"
        print(f"[TEST 3 PASS] Valid input: tool_name={result['tool_name']}")

    asyncio.run(test_valid_input())

    # Test 4: validate_input with INVALID data (missing required field)
    async def test_invalid_input():
        agent = SupervisorAgent()
        try:
            await agent.dispatch_tool(priority="high")  # missing tool_name
            print("[TEST 4 FAIL] Should have raised ValueError")
            sys.exit(1)
        except ValueError as e:
            assert "Entrada inválida" in str(e)
            print("[TEST 4 PASS] Invalid input correctly rejected: ValueError raised")

    asyncio.run(test_invalid_input())

    # Test 5: validate_output
    from datetime import datetime

    from sky_claw.core.contracts import validate_output

    class SupervisorAgent2:
        @validate_output("dispatch_tool")
        async def dispatch_tool(self, **kwargs):
            return {
                "tool_name": "test",
                "success": True,
                "created_at": datetime.utcnow().isoformat(),
            }

    async def test_valid_output():
        agent = SupervisorAgent2()
        result = await agent.dispatch_tool()
        assert result["success"] is True
        print(f"[TEST 5 PASS] Valid output: success={result['success']}")

    asyncio.run(test_valid_output())

    # Test 6: validate_contract (combined, single execution)
    from sky_claw.core.contracts import validate_contract

    call_count = 0

    class SupervisorAgent3:
        @validate_contract("dispatch_tool")
        async def dispatch_tool(self, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "tool_name": kwargs["tool_name"],
                "success": True,
                "created_at": datetime.utcnow().isoformat(),
            }

    async def test_full_contract():
        nonlocal call_count
        call_count = 0
        agent = SupervisorAgent3()
        result = await agent.dispatch_tool(tool_name="install", priority="critical")
        assert call_count == 1, f"Function executed {call_count} times (expected 1)"
        assert result["tool_name"] == "install"
        print(
            f"[TEST 6 PASS] Full contract: single execution, tool_name={result['tool_name']}"
        )

    asyncio.run(test_full_contract())

    # Test 7: No-contract method (pass-through)
    class UnknownAgent:
        @validate_input("unknown_method")
        async def unknown_method(self, x=1):
            return x

    async def test_passthrough():
        agent = UnknownAgent()
        result = await agent.unknown_method(x=42)
        assert result == 42
        print(f"[TEST 7 PASS] Pass-through for unregistered contract: {result}")

    asyncio.run(test_passthrough())

    print("\n" + "=" * 50)
    print("ALL 7 TESTS PASSED — Ticket 1.1 (C-03) VERIFIED")
    print("=" * 50)


if __name__ == "__main__":
    main()
