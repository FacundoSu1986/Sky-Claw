Feature: Generación de Grass Cache después de estabilizar el load order
  Como dispatcher de orquestación de Sky-Claw
  Quiero ejecutar el precache de grass sólo después de un LOOT confirmado
  Para respetar el orden Stage 5 → Stage 8 del SOP sin mutar un estado inválido

  # La postura HITL es parte de cada caso, NO del decorado: mientras vivió en el
  # Background, la rama de denegación era inexpresable y nunca se ejercitó.

  Background:
    Given que el entorno de Mod Organizer 2 usa rutas de prueba aisladas
    And que el journal real y el servicio de grass cache están disponibles

  Scenario: El agente ejecuta Grass Cache después de un LOOT commiteado
    Given que el operador aprueba la ejecución destructiva de "generate_grass_cache"
    And que LOOT tiene un FlightReport commiteado en el journal
    When el dispatcher despacha "generate_grass_cache" para el worldspace "Tamriel"
    Then el contrato normalizado responde con éxito y mensaje vacío
    And se solicita aprobación HITL para "generate_grass_cache"
    And el contrato reporta outcome "completed" y un cgid_count mayor que cero
    And se solicitan los locks "grass-cache" y "load-order" en ese orden
    And el journal registra "Grass precache" como commiteado
    And se publica el evento "pipeline.grass_cache.completed"
    And el perfil temporal se desmonta sin fallos de teardown

  Scenario: El guard rechaza Grass Cache cuando LOOT no está confirmado
    Given que el operador aprueba la ejecución destructiva de "generate_grass_cache"
    And que LOOT no tiene un FlightReport commiteado en el journal
    When el dispatcher despacha "generate_grass_cache" para el worldspace "Tamriel"
    Then el contrato normalizado responde con fallo
    And se solicita aprobación HITL para "generate_grass_cache"
    And el mensaje normalizado indica ejecutar "execute_loot_sorting"
    And el ritual no alcanza ninguna mutación

  Scenario: El operador deniega la aprobación y el ritual no toca nada
    Given que el operador deniega la ejecución destructiva de "generate_grass_cache"
    And que LOOT tiene un FlightReport commiteado en el journal
    When el dispatcher despacha "generate_grass_cache" para el worldspace "Tamriel"
    Then el contrato normalizado responde con fallo
    And se solicita aprobación HITL para "generate_grass_cache"
    And el fallo se atribuye a "HITLApprovalDenied"
    And el ritual no alcanza ninguna mutación

  Scenario: Sin HITLGuard cableado el gate deniega por política fail-closed
    Given que el dispatcher se cablea sin ningún HITLGuard
    And que LOOT tiene un FlightReport commiteado en el journal
    When el dispatcher despacha "generate_grass_cache" para el worldspace "Tamriel"
    Then el contrato normalizado responde con fallo
    And el fallo se atribuye a "HITLGateUnavailable"
    And el ritual no alcanza ninguna mutación

  Scenario: El bypass del guard de stage exige opt-in explícito
    Given que el operador aprueba la ejecución destructiva de "generate_grass_cache"
    And que LOOT no tiene un FlightReport commiteado en el journal
    When el dispatcher despacha "generate_grass_cache" para "Tamriel" con force_stage_guard
    Then el contrato normalizado responde con éxito y mensaje vacío
    And se solicita aprobación HITL para "generate_grass_cache"
    And se solicitan los locks "grass-cache" y "load-order" en ese orden

  # Estos escenarios afirman el contrato de orquestación con el runner mockeado.
  # El smoke real de NGIO/Skyrim permanece fuera de CI.
