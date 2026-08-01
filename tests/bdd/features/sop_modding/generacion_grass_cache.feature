Feature: Generación de Grass Cache después de estabilizar el load order
  Como dispatcher de orquestación de Sky-Claw
  Quiero ejecutar el precache de grass sólo después de un LOOT confirmado
  Para respetar el orden Stage 5 → Stage 8 del SOP sin mutar un estado inválido

  Background:
    Given que el entorno de Mod Organizer 2 usa rutas de prueba aisladas
    And que el journal real y el servicio de grass cache están disponibles
    And que el operador aprueba la ejecución destructiva de "generate_grass_cache"

  Scenario: El agente ejecuta Grass Cache después de un LOOT commiteado
    Given que LOOT tiene un FlightReport commiteado en el journal
    When el dispatcher despacha "generate_grass_cache" para el worldspace "Tamriel"
    Then el contrato responde con éxito y mensaje vacío
    And se solicita aprobación HITL para "generate_grass_cache"
    And el contrato reporta outcome "completed" y un cgid_count mayor que cero
    And se solicitan los locks "grass-cache" y "load-order" en ese orden
    And el journal registra "Grass precache" como commiteado
    And se publica el evento "pipeline.grass_cache.completed"
    And el perfil temporal se desmonta sin fallos de teardown

  Scenario: El guard rechaza Grass Cache cuando LOOT no está confirmado
    Given que LOOT no tiene un FlightReport commiteado en el journal
    When el dispatcher despacha "generate_grass_cache" para el worldspace "Tamriel"
    Then el contrato responde con fallo
    And se solicita aprobación HITL para "generate_grass_cache"
    And el mensaje indica ejecutar "execute_loot_sorting"
    And no se solicita ningún lock mutante
    And no se crea ningún runner del juego
    And no se prepara ni desmonta ningún perfil temporal
    And el journal no contiene una transacción de grass commiteada

  # Estos escenarios afirman el contrato de orquestación con el runner mockeado.
  # El smoke real de NGIO/Skyrim permanece fuera de CI.
