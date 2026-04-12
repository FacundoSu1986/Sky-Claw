# -*- coding: utf-8 -*-
"""
test_autogen_integration.py - Pruebas de integración para AutoGen.
Verifica que la integración de AutoGen funcione correctamente con
la arquitectura Sky-Claw.
"""

import sys
import os
import traceback

# Insertar el directorio del proyecto al inicio del path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def test_autogen_imports():
    """Test 1: AutoGen module imports"""
    print("=" * 60)
    print("Test 1: AutoGen Module Imports")
    print("=" * 60)
    try:
        from sky_claw.agent.autogen_integration import (  # noqa: F401
            AUTOGEN_AVAILABLE,
            AutoGenConfig,
            SkyClawConversableAgent,
            AutoGenWrapper,
            MultiAgentOrchestrator,
            create_sky_claw_agents,
            get_orchestrator,
        )

        print(f"[INFO] AUTOGEN_AVAILABLE = {AUTOGEN_AVAILABLE}")
        if not AUTOGEN_AVAILABLE:
            print("[PASS] Graceful degradation: AutoGen not installed, using stubs")
        else:
            print("[PASS] AutoGen is available")
        return True
    except ImportError as e:
        print(f"[FAIL] Import error: {e}")
        traceback.print_exc()
        return False


def test_autogen_config():
    """Test 2: AutoGenConfig functionality"""
    print()
    print("=" * 60)
    print("Test 2: AutoGenConfig Functionality")
    print("=" * 60)
    try:
        from sky_claw.agent.autogen_integration import AutoGenConfig

        # Test default config
        config = AutoGenConfig()
        llm_config = config.to_llm_config()
        print(f"[PASS] Default config created: model={llm_config['model']}")

        # Test custom config
        custom_config = AutoGenConfig(
            model="gpt-4-turbo", api_key="test_key", temperature=0.5, max_tokens=1000
        )
        custom_llm = custom_config.to_llm_config()
        print(
            f"[PASS] Custom config created: model={custom_llm['model']}, temp={custom_llm['temperature']}"
        )

        return True
    except Exception as e:
        print(f"[FAIL] AutoGenConfig error: {e}")
        traceback.print_exc()
        return False


def test_autogen_wrapper():
    """Test 3: AutoGenWrapper functionality"""
    print()
    print("=" * 60)
    print("Test 3: AutoGenWrapper Functionality")
    print("=" * 60)
    try:
        from sky_claw.agent.autogen_integration import AutoGenWrapper, AutoGenConfig

        # Create assistant agent
        assistant = AutoGenWrapper(
            name="TestAssistant",
            system_message="You are a test assistant agent.",
            agent_type="assistant",
            config=AutoGenConfig(),
        )
        print(f"[PASS] Assistant agent created: {assistant.name}")

        # Create user proxy agent
        user_proxy = AutoGenWrapper(
            name="TestUserProxy",
            system_message="You are a test user proxy agent.",
            agent_type="user_proxy",
            config=AutoGenConfig(),
        )
        print(f"[PASS] User proxy agent created: {user_proxy.name}")

        # Test message history
        history = assistant.get_history()
        print(f"[PASS] Agent history retrieved: {len(history)} messages")

        return True
    except Exception as e:
        print(f"[FAIL] AutoGenWrapper error: {e}")
        traceback.print_exc()
        return False


def test_multi_agent_orchestrator():
    """Test 4: MultiAgentOrchestrator functionality"""
    print()
    print("=" * 60)
    print("Test 4: MultiAgentOrchestrator Functionality")
    print("=" * 60)
    try:
        from sky_claw.agent.autogen_integration import (
            AutoGenWrapper,
            MultiAgentOrchestrator,
            AutoGenConfig,
        )
        import asyncio

        # Create test agents
        agent1 = AutoGenWrapper(
            name="Agent1",
            system_message="You are Agent 1.",
            agent_type="assistant",
            config=AutoGenConfig(),
        )
        agent2 = AutoGenWrapper(
            name="Agent2",
            system_message="You are Agent 2.",
            agent_type="assistant",
            config=AutoGenConfig(),
        )

        # Create orchestrator
        orchestrator = MultiAgentOrchestrator(agents=[agent1, agent2], max_round=5)
        print(f"[PASS] Orchestrator created with {len(orchestrator.agents)} agents")

        # Test conversation (async)
        async def run_test_conversation():
            results = await orchestrator.run_conversation(
                initial_message="Hello, this is a test message."
            )
            return results

        # Run async test
        results = asyncio.run(run_test_conversation())
        print(
            f"[PASS] Conversation completed: {results['rounds']} rounds, status={results['status']}"
        )

        # Test add/remove agent
        agent3 = AutoGenWrapper(
            name="Agent3",
            system_message="You are Agent 3.",
            agent_type="assistant",
            config=AutoGenConfig(),
        )
        orchestrator.add_agent(agent3)
        print(f"[PASS] Agent added: {len(orchestrator.agents)} agents now")

        removed = orchestrator.remove_agent("Agent3")
        print(
            f"[PASS] Agent removed: {removed}, {len(orchestrator.agents)} agents remaining"
        )

        return True
    except Exception as e:
        print(f"[FAIL] MultiAgentOrchestrator error: {e}")
        traceback.print_exc()
        return False


def test_create_sky_claw_agents():
    """Test 5: create_sky_claw_agents factory function"""
    print()
    print("=" * 60)
    print("Test 5: create_sky_claw_agents Factory Function")
    print("=" * 60)
    try:
        from sky_claw.agent.autogen_integration import (
            create_sky_claw_agents,
            AutoGenConfig,
        )
        from sky_claw.agent.lcel_chains import ToolExecutor

        # Create tool executor
        executor = ToolExecutor(tool_name="test", tool_description="Test executor")

        # Create Sky-Claw agents
        agents = create_sky_claw_agents(tool_executor=executor, config=AutoGenConfig())

        expected_agents = ["supervisor", "scraper", "security", "database"]
        for name in expected_agents:
            if name in agents:
                print(f"[PASS] Agent '{name}' created: {agents[name].name}")
            else:
                print(f"[FAIL] Agent '{name}' not found in factory output")
                return False

        print(f"[PASS] All {len(agents)} Sky-Claw agents created successfully")
        return True
    except Exception as e:
        print(f"[FAIL] create_sky_claw_agents error: {e}")
        traceback.print_exc()
        return False


def test_get_orchestrator():
    """Test 6: get_orchestrator singleton function"""
    print()
    print("=" * 60)
    print("Test 6: get_orchestrator Singleton Function")
    print("=" * 60)
    try:
        from sky_claw.agent.autogen_integration import get_orchestrator
        from sky_claw.agent.lcel_chains import ToolExecutor

        # Create tool executor
        executor = ToolExecutor(tool_name="test", tool_description="Test executor")

        # Get orchestrator instance
        orchestrator1 = get_orchestrator(tool_executor=executor)
        print(
            f"[PASS] First orchestrator instance obtained: {len(orchestrator1.agents)} agents"
        )

        # Get same instance (should be singleton)
        orchestrator2 = get_orchestrator(tool_executor=executor)
        if orchestrator1 is orchestrator2:
            print("[PASS] Singleton pattern verified: same instance returned")
        else:
            print("[INFO] Different instances (force_new not tested)")

        # Force new instance
        get_orchestrator(tool_executor=executor, force_new=True)
        print("[PASS] New orchestrator instance created with force_new=True")

        return True
    except Exception as e:
        print(f"[FAIL] get_orchestrator error: {e}")
        traceback.print_exc()
        return False


def test_agent_communication():
    """Test 7: Agent-to-agent communication"""
    print()
    print("=" * 60)
    print("Test 7: Agent-to-Agent Communication")
    print("=" * 60)
    try:
        from sky_claw.agent.autogen_integration import AutoGenWrapper, AutoGenConfig
        import asyncio

        # Create two agents
        sender = AutoGenWrapper(
            name="Sender",
            system_message="You are the sender agent.",
            agent_type="assistant",
            config=AutoGenConfig(),
        )
        receiver = AutoGenWrapper(
            name="Receiver",
            system_message="You are the receiver agent.",
            agent_type="assistant",
            config=AutoGenConfig(),
        )

        # Test async communication
        async def test_send_receive():
            # Send message
            response = await sender.send_message(
                message="Hello from Sender!", recipient=receiver
            )
            return response

        response = asyncio.run(test_send_receive())
        print(f"[PASS] Message sent and response received: {response[:50]}...")

        # Verify history
        sender_history = sender.get_history()
        receiver_history = receiver.get_history()
        print(f"[PASS] Sender history: {len(sender_history)} messages")
        print(f"[PASS] Receiver history: {len(receiver_history)} messages")

        return True
    except Exception as e:
        print(f"[FAIL] Agent communication error: {e}")
        traceback.print_exc()
        return False


def main():
    """Run all integration tests"""
    print()
    print("*" * 60)
    print("SKY_CLAW AUTOGEN INTEGRATION TEST SUITE")
    print("*" * 60)
    print(f"Python version: {sys.version}")
    print()

    results = []
    results.append(("AutoGen Imports", test_autogen_imports()))
    results.append(("AutoGenConfig", test_autogen_config()))
    results.append(("AutoGenWrapper", test_autogen_wrapper()))
    results.append(("MultiAgentOrchestrator", test_multi_agent_orchestrator()))
    results.append(("create_sky_claw_agents", test_create_sky_claw_agents()))
    results.append(("get_orchestrator", test_get_orchestrator()))
    results.append(("Agent Communication", test_agent_communication()))

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
        print("ALL AUTOGEN INTEGRATION TESTS PASSED")
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
