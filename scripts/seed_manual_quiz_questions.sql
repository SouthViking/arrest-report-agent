\set ON_ERROR_STOP on

-- Banco inicial de preguntas del Manual de Politicas y Procedimientos del LSSD.
--
-- Uso:
--   psql "$DATABASE_URL" \
--     --set=guild_id=1234674521416663070 \
--     --file=scripts/seed_manual_quiz_questions.sql
--
-- El seed es idempotente por (guild_id, slug):
-- - crea las preguntas que no existen;
-- - actualiza y aumenta la revision solo cuando cambia su contenido;
-- - reemplaza las alternativas por su version declarada aqui;
-- - no modifica preguntas de otras guilds ni slugs ajenos a este archivo.

\if :{?guild_id}
\else
  \echo 'ERROR: debe indicar --set=guild_id=<snowflake de Discord>'
  \quit
\endif

SELECT EXISTS (
    SELECT 1
    FROM discord_guilds
    WHERE discord_id = :'guild_id'::bigint
) AS manual_quiz_guild_exists
\gset

\if :manual_quiz_guild_exists
\else
  \echo 'ERROR: la guild indicada no existe en discord_guilds'
  \quit
\endif

BEGIN;

CREATE TEMPORARY TABLE manual_quiz_seed_items (
    slug text PRIMARY KEY,
    answer_mode text NOT NULL,
    category text NOT NULL,
    difficulty text NOT NULL,
    prompt text NOT NULL,
    explanation text NOT NULL,
    source_reference text NOT NULL,
    options jsonb NOT NULL
) ON COMMIT DROP;

INSERT INTO manual_quiz_seed_items (
    slug,
    answer_mode,
    category,
    difficulty,
    prompt,
    explanation,
    source_reference,
    options
)
SELECT
    item->>'slug',
    item->>'answer_mode',
    item->>'category',
    item->>'difficulty',
    item->>'prompt',
    item->>'explanation',
    item->>'source_reference',
    item->'options'
FROM jsonb_array_elements(
$manual_quiz_questions$
[
  {
    "slug": "general_character_believable",
    "answer_mode": "true_false",
    "category": "general",
    "difficulty": "basic",
    "prompt": "Un personaje de la facción debe comportarse de forma humana y creíble dentro del entorno de rol.",
    "explanation": "La interpretación debe ser coherente y verosímil; pertenecer a la facción no elimina las limitaciones humanas del personaje.",
    "source_reference": "Manual LSSD · I. Rol",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "general_secondary_law_faction",
    "answer_mode": "single_choice",
    "category": "general",
    "difficulty": "basic",
    "prompt": "¿Qué indica el manual respecto de mantener simultáneamente otro personaje en una facción policial secundaria?",
    "explanation": "El manual prohíbe mantener de forma simultánea un personaje secundario en otra facción policial.",
    "source_reference": "Manual LSSD · I. Rol",
    "options": [
      {"text": "Está prohibido", "correct": true},
      {"text": "Está permitido sin restricciones", "correct": false},
      {"text": "Solo requiere avisarlo por radio", "correct": false},
      {"text": "Depende del vehículo que utilice", "correct": false}
    ]
  },
  {
    "slug": "general_hierarchy_sheriff_undersheriff",
    "answer_mode": "single_choice",
    "category": "general",
    "difficulty": "basic",
    "prompt": "¿Cuál de estos rangos se encuentra por encima del Undersheriff en la jerarquía?",
    "explanation": "El Sheriff encabeza la estructura y se encuentra por encima del Undersheriff.",
    "source_reference": "Manual LSSD · V. Organización · Jerarquía",
    "options": [
      {"text": "Sheriff", "correct": true},
      {"text": "Captain", "correct": false},
      {"text": "Lieutenant", "correct": false},
      {"text": "Sergeant", "correct": false}
    ]
  },
  {
    "slug": "general_hierarchy_sergeant_lieutenant",
    "answer_mode": "true_false",
    "category": "general",
    "difficulty": "basic",
    "prompt": "En la jerarquía del departamento, Sergeant se encuentra por debajo de Lieutenant.",
    "explanation": "La jerarquía ubica a Lieutenant por encima de Sergeant.",
    "source_reference": "Manual LSSD · V. Organización · Jerarquía",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "legal_force_reasonable",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "basic",
    "prompt": "¿Qué principio debe orientar el nivel de fuerza usado por un ayudante?",
    "explanation": "La fuerza debe ser razonable y proporcional a la amenaza y a la necesidad concreta de la intervención.",
    "source_reference": "Manual LSSD · II. Políticas · Uso de la fuerza",
    "options": [
      {"text": "Razonabilidad y proporcionalidad frente a la amenaza", "correct": true},
      {"text": "Aplicar siempre el nivel máximo disponible", "correct": false},
      {"text": "Usar la misma fuerza en toda detención", "correct": false},
      {"text": "Depender únicamente del rango del ayudante", "correct": false}
    ]
  },
  {
    "slug": "legal_deadly_force_condition",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "intermediate",
    "prompt": "¿Cuándo puede justificarse el uso de fuerza letal?",
    "explanation": "Se reserva para defender la vida propia o de terceros ante una amenaza de muerte o lesiones graves.",
    "source_reference": "Manual LSSD · II. Políticas · Uso de la fuerza",
    "options": [
      {"text": "Ante una amenaza de muerte o lesiones graves", "correct": true},
      {"text": "Ante cualquier desacato verbal", "correct": false},
      {"text": "Siempre que una persona huya a pie", "correct": false},
      {"text": "Para acelerar una identificación", "correct": false}
    ]
  },
  {
    "slug": "legal_fleeing_warning",
    "answer_mode": "true_false",
    "category": "legal",
    "difficulty": "intermediate",
    "prompt": "Si las circunstancias lo permiten, debe advertirse antes de disparar contra un sospechoso que huye.",
    "explanation": "El manual exige efectuar una advertencia previa cuando resulte factible según la situación.",
    "source_reference": "Manual LSSD · II. Políticas · Uso de la fuerza",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "legal_recording_public",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "basic",
    "prompt": "Una persona graba una intervención desde un lugar permitido y no interfiere. ¿Qué corresponde?",
    "explanation": "La Primera Enmienda protege la grabación de la actividad policial mientras la persona no interfiera con el procedimiento.",
    "source_reference": "Manual LSSD · II. Políticas · Derechos constitucionales",
    "options": [
      {"text": "Permitir que continúe grabando", "correct": true},
      {"text": "Arrestarla únicamente por grabar", "correct": false},
      {"text": "Obligarla a borrar el material", "correct": false},
      {"text": "Retirar su dispositivo sin causa", "correct": false}
    ]
  },
  {
    "slug": "legal_fourth_amendment_probable_cause",
    "answer_mode": "true_false",
    "category": "legal",
    "difficulty": "intermediate",
    "prompt": "La protección frente a registros y detenciones irrazonables exige una justificación legal, como causa probable cuando corresponda.",
    "explanation": "La Cuarta Enmienda protege frente a actuaciones irrazonables y obliga a fundamentar legalmente la intervención.",
    "source_reference": "Manual LSSD · II. Políticas · Derechos constitucionales",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_taser_threat",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "¿Frente a qué condición se contempla el uso del táser?",
    "explanation": "El táser es un recurso menos letal limitado a situaciones en las que existe una amenaza que justifica su uso.",
    "source_reference": "Manual LSSD · III. Procedimientos · Armas menos letales",
    "options": [
      {"text": "Cuando existe una amenaza que lo justifica", "correct": true},
      {"text": "Para toda persona que demora en responder", "correct": false},
      {"text": "Para cualquier infracción de tránsito", "correct": false},
      {"text": "Solo para llamar la atención", "correct": false}
    ]
  },
  {
    "slug": "procedure_beanbag_armed_suspect",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "intermediate",
    "prompt": "Ante un sospechoso armado, ¿qué recurso menos letal señala el manual como preferente cuando la situación permite utilizarlo?",
    "explanation": "La escopeta beanbag se contempla como opción preferente frente a un sospechoso armado cuando su empleo es viable.",
    "source_reference": "Manual LSSD · III. Procedimientos · Armas menos letales",
    "options": [
      {"text": "Escopeta beanbag", "correct": true},
      {"text": "Extintor", "correct": false},
      {"text": "Bengala", "correct": false},
      {"text": "Arma de servicio como primera opción automática", "correct": false}
    ]
  },
  {
    "slug": "procedure_beanbag_not_immobilize",
    "answer_mode": "true_false",
    "category": "identify_error",
    "difficulty": "intermediate",
    "prompt": "Impactar a una persona con beanbag garantiza que quede completamente inmovilizada.",
    "explanation": "El impacto de beanbag no garantiza la inmovilización; las unidades deben mantener cobertura y control de la amenaza.",
    "source_reference": "Manual LSSD · III. Procedimientos · Armas menos letales",
    "options": [
      {"text": "Verdadero", "correct": false},
      {"text": "Falso", "correct": true}
    ]
  },
  {
    "slug": "procedure_pursuit_precaution",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "¿Con qué criterio general debe desarrollarse una persecución vehicular?",
    "explanation": "Las persecuciones deben conducirse de manera conservadora y precautoria, ponderando el riesgo de continuar.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones",
    "options": [
      {"text": "De manera conservadora y precautoria", "correct": true},
      {"text": "Buscando contacto a cualquier costo", "correct": false},
      {"text": "Sin considerar el tránsito circundante", "correct": false},
      {"text": "Siempre a la máxima velocidad posible", "correct": false}
    ]
  },
  {
    "slug": "procedure_pursuit_five_units",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "¿Cuántas unidades como máximo deben integrar la línea principal de una persecución?",
    "explanation": "El procedimiento limita la línea principal a cinco unidades para mantener coordinación y reducir riesgos.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones",
    "options": [
      {"text": "Cinco", "correct": true},
      {"text": "Dos", "correct": false},
      {"text": "Ocho", "correct": false},
      {"text": "No existe límite", "correct": false}
    ]
  },
  {
    "slug": "procedure_pursuit_no_overtake",
    "answer_mode": "true_false",
    "category": "identify_error",
    "difficulty": "intermediate",
    "prompt": "Una unidad puede adelantar a la primaria durante una persecución sin recibir autorización.",
    "explanation": "No corresponde adelantar dentro de la línea sin autorización; la formación debe conservar su coordinación.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones",
    "options": [
      {"text": "Verdadero", "correct": false},
      {"text": "Falso", "correct": true}
    ]
  },
  {
    "slug": "procedure_pursuit_radio_primary_supervisor",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "intermediate",
    "prompt": "¿Quiénes concentran principalmente las transmisiones radiales durante una persecución?",
    "explanation": "La unidad primaria y la supervisión deben concentrar la información esencial para evitar una frecuencia desordenada.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones",
    "options": [
      {"text": "La unidad primaria y la supervisión", "correct": true},
      {"text": "Todas las unidades al mismo tiempo", "correct": false},
      {"text": "Solo unidades que no participan", "correct": false},
      {"text": "Exclusivamente el sospechoso", "correct": false}
    ]
  },
  {
    "slug": "procedure_pursuit_risk_evaluation",
    "answer_mode": "true_false",
    "category": "scenario",
    "difficulty": "intermediate",
    "prompt": "La continuidad de una persecución debe reevaluarse si el riesgo para terceros supera la necesidad de capturar de inmediato.",
    "explanation": "El procedimiento exige ponderar permanentemente la seguridad pública y la necesidad de continuar.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_traffic_stop_radio",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "Al iniciar una parada de tránsito, ¿qué acción de coordinación debe realizarse?",
    "explanation": "La parada debe comunicarse por radio para dejar constancia de la ubicación y de la intervención.",
    "source_reference": "Manual LSSD · III. Procedimientos · Paradas de tránsito",
    "options": [
      {"text": "Informar la parada por radio", "correct": true},
      {"text": "Apagar la radio hasta finalizar", "correct": false},
      {"text": "Esperar a que el conductor avise", "correct": false},
      {"text": "Omitir toda referencia a la ubicación", "correct": false}
    ]
  },
  {
    "slug": "procedure_traffic_stop_mdc_owner",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "¿Qué consulta corresponde realizar mediante el MDC durante una parada vehicular?",
    "explanation": "El procedimiento incluye comprobar el registro del vehículo y la información de su propietario.",
    "source_reference": "Manual LSSD · III. Procedimientos · Paradas de tránsito",
    "options": [
      {"text": "El registro del vehículo y su propietario", "correct": true},
      {"text": "El clima de la semana siguiente", "correct": false},
      {"text": "La antigüedad de la patrulla", "correct": false},
      {"text": "La lista completa de empleados del condado", "correct": false}
    ]
  },
  {
    "slug": "procedure_traffic_stop_hands_visible",
    "answer_mode": "true_false",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "Durante el contacto inicial puede solicitarse al conductor que mantenga las manos visibles.",
    "explanation": "Mantener las manos visibles forma parte de las medidas de seguridad durante el acercamiento.",
    "source_reference": "Manual LSSD · III. Procedimientos · Paradas de tránsito",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_traffic_stop_documents",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "basic",
    "prompt": "Durante una parada vehicular regular, ¿qué documentación corresponde solicitar al conductor?",
    "explanation": "El procedimiento contempla solicitar la licencia y la documentación correspondiente al vehículo.",
    "source_reference": "Manual LSSD · III. Procedimientos · Paradas de tránsito",
    "options": [
      {"text": "Licencia y documentación del vehículo", "correct": true},
      {"text": "Contrato de vivienda", "correct": false},
      {"text": "Historial médico completo", "correct": false},
      {"text": "Credenciales laborales de todos los pasajeros", "correct": false}
    ]
  },
  {
    "slug": "procedure_forced_stop_primary_orders",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "intermediate",
    "prompt": "En una parada forzosa, ¿quién debe impartir las órdenes verbales principales?",
    "explanation": "La unidad primaria dirige las órdenes para evitar instrucciones contradictorias entre los intervinientes.",
    "source_reference": "Manual LSSD · III. Procedimientos · Paradas forzosas",
    "options": [
      {"text": "La unidad primaria", "correct": true},
      {"text": "Todos los ayudantes a la vez", "correct": false},
      {"text": "La última unidad en llegar", "correct": false},
      {"text": "Cualquier civil presente", "correct": false}
    ]
  },
  {
    "slug": "procedure_forced_stop_crossfire",
    "answer_mode": "true_false",
    "category": "identify_error",
    "difficulty": "intermediate",
    "prompt": "En una parada forzosa conviene agrupar las unidades sin separación, aunque se creen líneas de fuego cruzado.",
    "explanation": "Las unidades deben mantener una formación y distancia que reduzcan el riesgo de fuego cruzado.",
    "source_reference": "Manual LSSD · III. Procedimientos · Paradas forzosas",
    "options": [
      {"text": "Verdadero", "correct": false},
      {"text": "Falso", "correct": true}
    ]
  },
  {
    "slug": "procedure_forced_stop_teams",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "intermediate",
    "prompt": "¿Qué distribución de funciones contempla una parada forzosa al extraer y asegurar a los ocupantes?",
    "explanation": "Se organizan funciones de arresto y cobertura para que unas unidades aseguren mientras otras protegen la intervención.",
    "source_reference": "Manual LSSD · III. Procedimientos · Paradas forzosas",
    "options": [
      {"text": "Equipos de arresto y de cobertura", "correct": true},
      {"text": "Solo un equipo administrativo", "correct": false},
      {"text": "Únicamente un fotógrafo", "correct": false},
      {"text": "No se asignan funciones", "correct": false}
    ]
  },
  {
    "slug": "procedure_citation_electronic_sequence",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "intermediate",
    "prompt": "Al emitir una citación electrónica, ¿qué debe ocurrir antes de finalizar la intervención?",
    "explanation": "El ayudante debe completar y comunicar la citación siguiendo la secuencia establecida antes de liberar al conductor.",
    "source_reference": "Manual LSSD · III. Procedimientos · Citaciones",
    "options": [
      {"text": "Completar y explicar la citación antes de liberar", "correct": true},
      {"text": "Liberar primero y completar los datos otro día", "correct": false},
      {"text": "Omitir la identificación del infractor", "correct": false},
      {"text": "Destruir el registro al terminar", "correct": false}
    ]
  },
  {
    "slug": "procedure_arrest_gloves",
    "answer_mode": "true_false",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "El procedimiento de arresto contempla utilizar guantes al registrar y manipular evidencia o pertenencias.",
    "explanation": "Los guantes reducen contaminación y protegen tanto al ayudante como a la evidencia manipulada.",
    "source_reference": "Manual LSSD · III. Procedimientos · Arrestos",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_arrest_intent_cause_authority",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "intermediate",
    "prompt": "Al decidir un arresto y cuando la situación lo permite, ¿qué debe comunicar el ayudante?",
    "explanation": "Debe informar la intención de arrestar, la causa y la autoridad bajo la que actúa.",
    "source_reference": "Manual LSSD · III. Procedimientos · Arrestos",
    "options": [
      {"text": "Intención, causa y autoridad del arresto", "correct": true},
      {"text": "Solo su número de teléfono", "correct": false},
      {"text": "Únicamente el destino final de la patrulla", "correct": false},
      {"text": "Nada, en ninguna circunstancia", "correct": false}
    ]
  },
  {
    "slug": "procedure_arrest_exigent_exception",
    "answer_mode": "true_false",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Una situación urgente puede justificar que la comunicación formal del arresto se complete una vez controlado el peligro inmediato.",
    "explanation": "Las circunstancias exigentes permiten priorizar el control y la seguridad antes de completar la explicación formal.",
    "source_reference": "Manual LSSD · III. Procedimientos · Arrestos",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_ambulance_bullet_wound",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "basic",
    "prompt": "Una persona presenta una herida por proyectil. ¿Qué recurso médico corresponde solicitar?",
    "explanation": "Las heridas por proyectil requieren transporte o asistencia de ambulancia conforme al procedimiento.",
    "source_reference": "Manual LSSD · III. Procedimientos · Transporte en ambulancia",
    "options": [
      {"text": "Ambulancia", "correct": true},
      {"text": "Ningún recurso médico", "correct": false},
      {"text": "Solo una grúa", "correct": false},
      {"text": "Esperar indefinidamente sin evaluar", "correct": false}
    ]
  },
  {
    "slug": "procedure_ambulance_less_lethal",
    "answer_mode": "true_false",
    "category": "scenario",
    "difficulty": "intermediate",
    "prompt": "Una persona alcanzada por táser, Arwen u otro recurso menos letal debe ser evaluada médicamente según el procedimiento.",
    "explanation": "El impacto o descarga de recursos menos letales figura entre las condiciones que requieren evaluación o transporte médico.",
    "source_reference": "Manual LSSD · III. Procedimientos · Transporte en ambulancia",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_ambulance_head_strike",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "intermediate",
    "prompt": "Tras una intervención, el detenido recibió un golpe en la cabeza y presenta lesiones aparentes. ¿Qué corresponde?",
    "explanation": "Los golpes en la cabeza y las lesiones aparentes requieren evaluación médica y pueden exigir transporte en ambulancia.",
    "source_reference": "Manual LSSD · III. Procedimientos · Transporte en ambulancia",
    "options": [
      {"text": "Solicitar evaluación o transporte médico", "correct": true},
      {"text": "Ignorar el golpe si puede hablar", "correct": false},
      {"text": "Pedirle que conduzca por su cuenta", "correct": false},
      {"text": "Completar únicamente una citación", "correct": false}
    ]
  },
  {
    "slug": "procedure_k9_supported_uses",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "intermediate",
    "prompt": "¿Cuál es un uso contemplado para una unidad K9?",
    "explanation": "Las unidades K9 pueden apoyar la detección de narcóticos, localización de sospechosos o evidencia y búsquedas de personas.",
    "source_reference": "Manual LSSD · III. Procedimientos · Unidad canina",
    "options": [
      {"text": "Localizar narcóticos, sospechosos, evidencia o personas", "correct": true},
      {"text": "Emitir licencias de conducir", "correct": false},
      {"text": "Reparar vehículos oficiales", "correct": false},
      {"text": "Sustituir toda investigación humana", "correct": false}
    ]
  },
  {
    "slug": "procedure_k9_narcotics",
    "answer_mode": "true_false",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "La detección de narcóticos es una de las funciones que puede cumplir una unidad K9 entrenada.",
    "explanation": "El manual contempla expresamente el apoyo canino en la detección de narcóticos.",
    "source_reference": "Manual LSSD · III. Procedimientos · Unidad canina",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_k9_no_guarantee",
    "answer_mode": "true_false",
    "category": "identify_error",
    "difficulty": "intermediate",
    "prompt": "El despliegue de una unidad K9 garantiza por sí solo que la búsqueda tendrá un resultado positivo.",
    "explanation": "El apoyo canino no garantiza un hallazgo; sus resultados deben interpretarse dentro del procedimiento completo.",
    "source_reference": "Manual LSSD · III. Procedimientos · Unidad canina",
    "options": [
      {"text": "Verdadero", "correct": false},
      {"text": "Falso", "correct": true}
    ]
  },
  {
    "slug": "procedure_seb_multiple_armed_suspects",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "intermediate",
    "prompt": "Una situación involucra dos o más sospechosos armados y presenta alto riesgo. ¿Qué recurso especializado puede solicitarse?",
    "explanation": "Las situaciones de alto riesgo con múltiples sospechosos armados se encuentran entre los supuestos de activación de SEB.",
    "source_reference": "Manual LSSD · III. Procedimientos · Solicitud de SEB",
    "options": [
      {"text": "Special Enforcement Bureau (SEB)", "correct": true},
      {"text": "Solo mantenimiento vial", "correct": false},
      {"text": "Únicamente una grúa", "correct": false},
      {"text": "Ningún apoyo adicional", "correct": false}
    ]
  },
  {
    "slug": "procedure_seb_hostage",
    "answer_mode": "true_false",
    "category": "scenario",
    "difficulty": "basic",
    "prompt": "Una situación con rehenes puede justificar una solicitud de SEB.",
    "explanation": "Las situaciones con rehenes forman parte de los incidentes de alto riesgo contemplados para SEB.",
    "source_reference": "Manual LSSD · III. Procedimientos · Solicitud de SEB",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "procedure_seb_no_member_arrives",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Se solicitó SEB, pero ningún ayudante de esa unidad llega a la escena. ¿Quién mantiene la responsabilidad de proceder?",
    "explanation": "Si SEB no llega, quien está a cargo de la escena debe continuar gestionando la situación conforme a los recursos disponibles.",
    "source_reference": "Manual LSSD · III. Procedimientos · Solicitud de SEB",
    "options": [
      {"text": "El encargado de la escena", "correct": true},
      {"text": "Nadie puede realizar ninguna acción", "correct": false},
      {"text": "El primer civil que se presente", "correct": false},
      {"text": "La intervención se elimina automáticamente", "correct": false}
    ]
  },
  {
    "slug": "procedure_seb_tactical_command",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "Cuando SEB interviene, ¿quién asume el mando táctico especializado?",
    "explanation": "El ayudante de mayor rango perteneciente a SEB asume el mando táctico de la operación especializada.",
    "source_reference": "Manual LSSD · III. Procedimientos · Solicitud de SEB",
    "options": [
      {"text": "El integrante de SEB de mayor rango", "correct": true},
      {"text": "El último integrante de SEB en llegar", "correct": false},
      {"text": "Cualquier espectador", "correct": false},
      {"text": "La persona investigada", "correct": false}
    ]
  },
  {
    "slug": "procedure_evidence_gloves_package",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "¿Cómo debe manipularse y conservarse evidencia física recogida en una escena?",
    "explanation": "Debe manipularse con guantes y guardarse en un envase o bolsa apropiada para preservar su integridad.",
    "source_reference": "Manual LSSD · III. Procedimientos · Evidencia",
    "options": [
      {"text": "Con guantes y en un envase o bolsa apropiada", "correct": true},
      {"text": "Con las manos desnudas y sin embalaje", "correct": false},
      {"text": "Mezclada con pertenencias personales", "correct": false},
      {"text": "Abandonada en el vehículo patrulla", "correct": false}
    ]
  },
  {
    "slug": "procedure_evidence_label",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "¿Qué identificación básica debe llevar una bolsa de evidencia según el manual?",
    "explanation": "La bolsa debe rotularse con el nombre y apellido correspondientes y la fecha de la intervención.",
    "source_reference": "Manual LSSD · III. Procedimientos · Evidencia",
    "options": [
      {"text": "Nombre, apellido y fecha", "correct": true},
      {"text": "Solo el color de la patrulla", "correct": false},
      {"text": "Únicamente la hora", "correct": false},
      {"text": "No requiere identificación", "correct": false}
    ]
  },
  {
    "slug": "procedure_evidence_storage",
    "answer_mode": "true_false",
    "category": "procedure",
    "difficulty": "basic",
    "prompt": "La evidencia recogida debe almacenarse en las dependencias destinadas por el LSSD.",
    "explanation": "La preservación exige trasladar y almacenar la evidencia en el lugar institucional correspondiente.",
    "source_reference": "Manual LSSD · III. Procedimientos · Evidencia",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "radio_response_code_3",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué significa responder en código 3?",
    "explanation": "Código 3 indica una respuesta de emergencia utilizando sirenas y luces.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de respuesta",
    "options": [
      {"text": "Emergencia, con sirenas y luces", "correct": true},
      {"text": "Investigación de campo", "correct": false},
      {"text": "Reanudar labores", "correct": false},
      {"text": "No se precisa más asistencia", "correct": false}
    ]
  },
  {
    "slug": "radio_response_code_4",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué comunica un código 4?",
    "explanation": "Código 4 señala que la situación está controlada y no se requiere más asistencia.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de respuesta",
    "options": [
      {"text": "No se precisa más asistencia", "correct": true},
      {"text": "Emergencia con sirenas", "correct": false},
      {"text": "Persona armada", "correct": false},
      {"text": "Solicitar ambulancia", "correct": false}
    ]
  },
  {
    "slug": "radio_response_code_5",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "¿Qué instrucción transmite un código 5?",
    "explanation": "Código 5 ordena que todas las unidades permanezcan alejadas de un área concreta.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de respuesta",
    "options": [
      {"text": "Mantener a todas las unidades alejadas del área", "correct": true},
      {"text": "Reanudar las labores habituales", "correct": false},
      {"text": "Comprobar el registro de un vehículo", "correct": false},
      {"text": "Informar la ubicación actual", "correct": false}
    ]
  },
  {
    "slug": "radio_response_code_6",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿A qué corresponde el código 6?",
    "explanation": "Código 6 identifica una investigación de campo.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de respuesta",
    "options": [
      {"text": "Investigación de campo", "correct": true},
      {"text": "Homicidio", "correct": false},
      {"text": "Alarma silenciosa", "correct": false},
      {"text": "Asignación finalizada", "correct": false}
    ]
  },
  {
    "slug": "radio_response_code_9",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "¿Para qué se utiliza el código 9?",
    "explanation": "Código 9 se emplea para coordinarse antes de realizar una parada.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de respuesta",
    "options": [
      {"text": "Coordinación previa a una parada", "correct": true},
      {"text": "Declarar una asignación terminada", "correct": false},
      {"text": "Pedir que se repita una transmisión", "correct": false},
      {"text": "Informar un homicidio", "correct": false}
    ]
  },
  {
    "slug": "radio_response_code_14",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "Tras finalizar un incidente grande, ¿qué código libera a las unidades para reanudar sus labores?",
    "explanation": "Código 14 indica reanudar labores y permite liberar a las unidades del incidente.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de respuesta",
    "options": [
      {"text": "Código 14", "correct": true},
      {"text": "Código 3", "correct": false},
      {"text": "Código 5", "correct": false},
      {"text": "Código 9", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_4",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué significa 10-4?",
    "explanation": "10-4 confirma que la información fue copiada o comprendida.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "Copiado", "correct": true},
      {"text": "Repita la información", "correct": false},
      {"text": "Ubicación actual", "correct": false},
      {"text": "Asignación finalizada", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_9",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "No entendiste una transmisión y necesitas que se repita. ¿Qué código corresponde?",
    "explanation": "10-9 solicita repetir la información transmitida.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "10-9", "correct": true},
      {"text": "10-4", "correct": false},
      {"text": "10-20", "correct": false},
      {"text": "10-98", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_15",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Cuándo se informa un 10-15?",
    "explanation": "10-15 informa que un sospechoso está bajo custodia, concretamente cuando queda esposado.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "Cuando el sospechoso queda esposado y bajo custodia", "correct": true},
      {"text": "Al llegar a una propiedad", "correct": false},
      {"text": "Al cancelar la última información", "correct": false},
      {"text": "Al iniciar una llamada telefónica", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_20",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué información solicita o comunica un 10-20?",
    "explanation": "10-20 corresponde a la ubicación actual de una unidad o persona.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "Ubicación actual", "correct": true},
      {"text": "Registro vehicular", "correct": false},
      {"text": "Mensaje urgente", "correct": false},
      {"text": "Sospechoso esposado", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_22",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "¿Qué significa 10-22?",
    "explanation": "10-22 deja sin efecto o cancela la última información transmitida.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "Cancelar la última información", "correct": true},
      {"text": "Contactar por teléfono", "correct": false},
      {"text": "Llegar a la escena", "correct": false},
      {"text": "Volver a una ubicación", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_28",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "¿Para qué se utiliza 10-28?",
    "explanation": "10-28 solicita verificar el registro de un vehículo o las órdenes asociadas a un sujeto.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "Verificar registro vehicular u órdenes de un sujeto", "correct": true},
      {"text": "Solicitar una ambulancia", "correct": false},
      {"text": "Informar una carrera callejera", "correct": false},
      {"text": "Despejar una zona con código 5", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_33",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "advanced",
    "prompt": "¿Qué condición acompaña el uso de 10-33?",
    "explanation": "10-33 identifica un mensaje urgente y debe acompañarse de una solicitud de parche.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "Es un mensaje urgente y requiere solicitar parche", "correct": true},
      {"text": "Es una transmisión rutinaria sin prioridad", "correct": false},
      {"text": "Se usa únicamente para finalizar turno", "correct": false},
      {"text": "Sustituye el código de ubicación", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_97",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "Una unidad acaba de llegar a la escena. ¿Qué código debe informar junto con la calle o propiedad?",
    "explanation": "10-97 comunica la llegada a la escena e incluye la referencia de calle o propiedad.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "10-97", "correct": true},
      {"text": "10-15", "correct": false},
      {"text": "10-22", "correct": false},
      {"text": "10-28", "correct": false}
    ]
  },
  {
    "slug": "radio_ten_98",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué significa 10-98?",
    "explanation": "10-98 comunica que la asignación ha finalizado.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos 10",
    "options": [
      {"text": "Asignación finalizada", "correct": true},
      {"text": "Ubicación actual", "correct": false},
      {"text": "Sospechoso en custodia", "correct": false},
      {"text": "Repita la información", "correct": false}
    ]
  },
  {
    "slug": "radio_situation_187",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué situación representa el código 187?",
    "explanation": "El código de situación 187 corresponde a homicidio.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de situación",
    "options": [
      {"text": "Homicidio", "correct": true},
      {"text": "Secuestro", "correct": false},
      {"text": "Vandalismo", "correct": false},
      {"text": "Incendio", "correct": false}
    ]
  },
  {
    "slug": "radio_situation_207",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué situación representa el código 207?",
    "explanation": "El código de situación 207 corresponde a secuestro.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de situación",
    "options": [
      {"text": "Secuestro", "correct": true},
      {"text": "Homicidio", "correct": false},
      {"text": "Disturbios", "correct": false},
      {"text": "Robo de vehículo", "correct": false}
    ]
  },
  {
    "slug": "radio_situation_211",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "¿Qué describe el código 211?",
    "explanation": "211 corresponde a robo entendido como sustracción mediante violencia.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de situación",
    "options": [
      {"text": "Robo con violencia", "correct": true},
      {"text": "Hurto sin violencia", "correct": false},
      {"text": "Incendio provocado", "correct": false},
      {"text": "Invasión de propiedad", "correct": false}
    ]
  },
  {
    "slug": "radio_situation_417a",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "intermediate",
    "prompt": "¿Qué informa el código 417A?",
    "explanation": "417A corresponde a una persona con cuchillo.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de situación",
    "options": [
      {"text": "Persona con cuchillo", "correct": true},
      {"text": "Persona atrincherada", "correct": false},
      {"text": "Disturbio de pandillas", "correct": false},
      {"text": "Disparos ilegales", "correct": false}
    ]
  },
  {
    "slug": "radio_situation_998",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "advanced",
    "prompt": "¿Qué comunica el código 998?",
    "explanation": "998 indica que un ayudante se encuentra envuelto en disparos.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de situación",
    "options": [
      {"text": "Ayudante envuelto en disparos", "correct": true},
      {"text": "Ayudante solicita ubicación", "correct": false},
      {"text": "Asignación finalizada", "correct": false},
      {"text": "Investigación de campo", "correct": false}
    ]
  },
  {
    "slug": "radio_situation_999_scc",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "advanced",
    "prompt": "¿Qué particularidad tiene el código 999?",
    "explanation": "999 comunica que un ayudante necesita ayuda urgente y todas las unidades responden, pero su uso está reservado a SCC.",
    "source_reference": "Manual LSSD · IV. Comunicación · Códigos de situación",
    "options": [
      {"text": "Solicita ayuda urgente y solo SCC utiliza el código", "correct": true},
      {"text": "Es de uso libre para cualquier transmisión rutinaria", "correct": false},
      {"text": "Solo significa asignación finalizada", "correct": false},
      {"text": "Identifica una alarma silenciosa", "correct": false}
    ]
  },
  {
    "slug": "radio_phonetic_adam",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué palabra representa la letra A en el alfabeto fonético utilizado por el departamento?",
    "explanation": "La letra A se comunica como Adam.",
    "source_reference": "Manual LSSD · IV. Comunicación · Alfabeto fonético",
    "options": [
      {"text": "Adam", "correct": true},
      {"text": "Ocean", "correct": false},
      {"text": "Sam", "correct": false},
      {"text": "Zebra", "correct": false}
    ]
  },
  {
    "slug": "radio_phonetic_ocean",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué letra representa Ocean en el alfabeto fonético utilizado por el departamento?",
    "explanation": "Ocean representa la letra O.",
    "source_reference": "Manual LSSD · IV. Comunicación · Alfabeto fonético",
    "options": [
      {"text": "O", "correct": true},
      {"text": "A", "correct": false},
      {"text": "S", "correct": false},
      {"text": "Z", "correct": false}
    ]
  },
  {
    "slug": "radio_phonetic_sam",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "¿Qué palabra representa la letra S en el alfabeto fonético utilizado por el departamento?",
    "explanation": "La letra S se comunica como Sam.",
    "source_reference": "Manual LSSD · IV. Comunicación · Alfabeto fonético",
    "options": [
      {"text": "Sam", "correct": true},
      {"text": "Sierra", "correct": false},
      {"text": "Susan", "correct": false},
      {"text": "South", "correct": false}
    ]
  },
  {
    "slug": "radio_phonetic_zebra",
    "answer_mode": "true_false",
    "category": "radio_code",
    "difficulty": "basic",
    "prompt": "En el alfabeto fonético utilizado por el departamento, la letra Z se comunica como Zebra.",
    "explanation": "Zebra es la palabra asignada a la letra Z.",
    "source_reference": "Manual LSSD · IV. Comunicación · Alfabeto fonético",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "reports_first_person_past",
    "answer_mode": "single_choice",
    "category": "reports",
    "difficulty": "basic",
    "prompt": "¿Qué perspectiva y tiempo verbal se esperan principalmente en la narrativa de un reporte de arresto?",
    "explanation": "La guía indica redactar en primera persona y narrar hechos ya ocurridos principalmente en pasado.",
    "source_reference": "Manual LSSD · VI. Guías · Reportes de arresto",
    "options": [
      {"text": "Primera persona y principalmente en pasado", "correct": true},
      {"text": "Tercera persona y siempre en futuro", "correct": false},
      {"text": "Segunda persona y solo en presente", "correct": false},
      {"text": "Cualquier perspectiva sin consistencia", "correct": false}
    ]
  },
  {
    "slug": "reports_structure_flexible",
    "answer_mode": "true_false",
    "category": "reports",
    "difficulty": "intermediate",
    "prompt": "La estructura sugerida para la narrativa puede adaptarse si el reporte sigue siendo claro, completo y cronológico.",
    "explanation": "La guía prioriza que los hechos sean comprensibles y suficientes; no exige convertir cada reporte en una plantilla literaria rígida.",
    "source_reference": "Manual LSSD · VI. Guías · Reportes de arresto",
    "options": [
      {"text": "Verdadero", "correct": true},
      {"text": "Falso", "correct": false}
    ]
  },
  {
    "slug": "reports_911_optional",
    "answer_mode": "true_false",
    "category": "reports",
    "difficulty": "intermediate",
    "prompt": "Todo reporte de arresto debe incluir obligatoriamente un número de llamada 911, aunque la intervención no se originara por una llamada.",
    "explanation": "La referencia a una llamada 911 es contextual y solo corresponde cuando realmente existe; no es obligatoria en todos los reportes.",
    "source_reference": "Manual LSSD · VI. Guías · Reportes de arresto",
    "options": [
      {"text": "Verdadero", "correct": false},
      {"text": "Falso", "correct": true}
    ]
  },
  {
    "slug": "reports_evidence_bag_reference",
    "answer_mode": "single_choice",
    "category": "reports",
    "difficulty": "intermediate",
    "prompt": "Si durante el arresto se incauta evidencia física y se almacena en una bolsa, ¿qué referencia debe conservar el reporte?",
    "explanation": "El reporte debe vincular la evidencia física con la bolsa o registro correspondiente para mantener trazabilidad.",
    "source_reference": "Manual LSSD · VI. Guías · Reportes de arresto",
    "options": [
      {"text": "La referencia o número de la bolsa de evidencia", "correct": true},
      {"text": "Solo el color del uniforme", "correct": false},
      {"text": "El kilometraje de todas las patrullas", "correct": false},
      {"text": "Ninguna referencia", "correct": false}
    ]
  },
  {
    "slug": "advanced_force_active_resistance_assessment",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un detenido ignora órdenes verbales, mantiene las manos visibles y no amenaza ni se resiste físicamente. ¿Qué evaluación aplica mejor antes de usar un táser?",
    "explanation": "La mera desobediencia no basta. Las armas menos letales exigen resistencia activa y una amenaza para el deputy o terceros.",
    "source_reference": "Manual LSSD · II. Políticas · Uso de armas menos letales",
    "options": [
      {"text": "Continuar con control verbal y reevaluar; todavía no se justifican armas menos letales", "correct": true},
      {"text": "Advertir una última vez y usar el táser si persiste, pues la desobediencia sostenida ya permite clasificarla como resistencia activa", "correct": false},
      {"text": "Cambiar a beanbag por su mayor distancia, aunque la conducta y el nivel de amenaza no hayan variado", "correct": false},
      {"text": "Autorizar cualquiera de los dos medios menos letales si un segundo deputy mantiene cobertura", "correct": false}
    ]
  },
  {
    "slug": "advanced_force_blade_distance_choice",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un sospechoso con arma blanca se resiste activamente y amenaza a terceros desde una distancia que permite una opción menos letal. ¿Qué recurso prioriza el manual?",
    "explanation": "En circunstancias que admiten una respuesta menos letal, la beanbag es preferible al táser ante un sospechoso armado con arma blanca por su mayor alcance.",
    "source_reference": "Manual LSSD · II. Políticas · Uso de armas menos letales",
    "options": [
      {"text": "Beanbag, manteniendo cobertura y una evaluación constante de la amenaza", "correct": true},
      {"text": "Táser, porque su aturdimiento ofrece más control aunque obligue a reducir la distancia", "correct": false},
      {"text": "Mantener solo órdenes verbales hasta que el sospechoso inicie un ataque efectivo", "correct": false},
      {"text": "Arma de servicio, porque la presencia del arma blanca vuelve innecesaria la alternativa menos letal", "correct": false}
    ]
  },
  {
    "slug": "advanced_force_fleeing_threat_threshold",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "Un sospechoso huye después de un delito grave, pero no hay hechos que indiquen que represente una amenaza significativa de muerte o lesiones graves. ¿Qué conclusión corresponde sobre la fuerza letal?",
    "explanation": "La gravedad del delito o la fuga por sí solas no bastan: se requiere causa probable de una amenaza significativa de muerte o lesiones graves.",
    "source_reference": "Manual LSSD · II. Políticas · Uso de la fuerza",
    "options": [
      {"text": "No está justificada solo por la fuga; debe continuarse con medios proporcionales", "correct": true},
      {"text": "Puede justificarse tras identificarse y advertir la intención de disparar, aunque no aparezca una amenaza adicional", "correct": false},
      {"text": "Puede justificarse si el delito es grave y un supervisor confirma la necesidad de impedir la fuga", "correct": false},
      {"text": "Puede justificarse cuando no exista un medio menos letal con alcance suficiente para detenerlo", "correct": false}
    ]
  },
  {
    "slug": "advanced_constitution_recording_interference",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "Una persona graba una detención desde una posición permitida. Luego cruza el perímetro e interfiere con el equipo de arresto. ¿Qué distingue correctamente ambas conductas?",
    "explanation": "Documentar la actuación está protegido mientras no interfiera; cruzar el perímetro y obstaculizar permite intervenir por esa conducta, no por la grabación.",
    "source_reference": "Manual LSSD · II. Políticas · Principios constitucionales",
    "options": [
      {"text": "La grabación inicial es legítima; la interferencia posterior sí puede ser controlada", "correct": true},
      {"text": "Al cruzar el perímetro pierde también la protección de la grabación previa y puede ordenarse eliminarla", "correct": false},
      {"text": "El dispositivo puede retenerse como evidencia de la interferencia, aunque no exista otra causa para incautarlo", "correct": false},
      {"text": "Puede retirarse a la persona del perímetro, pero debe exigírsele que deje de grabar el resto del procedimiento", "correct": false}
    ]
  },
  {
    "slug": "advanced_constitution_reasonable_suspicion_scope",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "Un deputy reúne hechos objetivos que superan una intuición, pero todavía no alcanzan causa probable. ¿Qué alcance se ajusta mejor a esa sospecha razonable?",
    "explanation": "El manual diferencia la sospecha razonable, que permite intervenciones breves y acotadas, de la causa probable requerida para actuaciones más invasivas.",
    "source_reference": "Manual LSSD · II. Políticas · Principios constitucionales",
    "options": [
      {"text": "Una detención breve y un registro menor relacionado con la actividad sospechada", "correct": true},
      {"text": "Un registro completo del vehículo y la propiedad para confirmar o descartar cualquier delito", "correct": false},
      {"text": "Un arresto preventivo mientras se solicita que un juez determine después la causa probable", "correct": false},
      {"text": "Solo observación a distancia, porque toda detención exige causa probable", "correct": false}
    ]
  },
  {
    "slug": "advanced_incident_first_unit_code4",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "La primera unidad mantiene el manejo de una escena. Llega un deputy de mayor experiencia, pero sin asumir formalmente el incidente, y considera que ya no hace falta apoyo. ¿Quién debe informar código 4?",
    "explanation": "El código 4 corresponde al Incident Commander que conserva el manejo. La experiencia o llegada posterior no transfiere automáticamente esa responsabilidad.",
    "source_reference": "Manual LSSD · III. Procedimientos · Respecto a toda situación",
    "options": [
      {"text": "La unidad que conserva el manejo como Incident Commander", "correct": true},
      {"text": "La unidad de mayor rango presente, después de comprobar que no quedan tareas pendientes", "correct": false},
      {"text": "SCC, una vez que cualquier unidad de la escena recomiende liberar los apoyos", "correct": false},
      {"text": "La unidad que el IC haya destinado al perímetro, por ser quien mejor observa el estado general", "correct": false}
    ]
  },
  {
    "slug": "advanced_pursuit_sixth_unit_position",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Una persecución ya tiene cinco patrullas en cola y un supervisor. Una sexta patrulla ordinaria llega al sector. ¿Cómo debe integrarse?",
    "explanation": "El máximo es de cinco carros en cola más un supervisor. Las unidades adicionales deben circular por calles aledañas y pueden quedar disponibles para funciones autorizadas, como pinchos.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones",
    "options": [
      {"text": "Mantenerse por calles aledañas sin sumarse a la cola", "correct": true},
      {"text": "Quedar detrás del supervisor como sexta patrulla, manteniendo una separación mayor con la cola", "correct": false},
      {"text": "Circular en paralelo y reincorporarse a la cola cuando la ruta permita adelantar sin riesgo", "correct": false},
      {"text": "Tomar una ruta adelantada para preparar pinchos, aun sin una asignación o autorización específica", "correct": false}
    ]
  },
  {
    "slug": "advanced_pursuit_secondary_radio_hazard",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Durante una persecución, una unidad secundaria detecta un vehículo civil detenido en la trayectoria inmediata. La primaria está transmitiendo. ¿Qué debe hacer la secundaria?",
    "explanation": "Aunque normalmente solo hablan la unidad primaria o un supervisor, otra unidad puede transmitir para advertir un peligro inminente.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones",
    "options": [
      {"text": "Advertir brevemente el peligro inminente por radio", "correct": true},
      {"text": "Transmitir el peligro únicamente después de que un supervisor confirme que puede interrumpir a la primaria", "correct": false},
      {"text": "Esperar una pausa de la primaria y describir el peligro una vez que el vehículo perseguido ya lo haya superado", "correct": false},
      {"text": "Actuar desde una calle lateral sin transmitir, para no saturar la frecuencia con información de una unidad secundaria", "correct": false}
    ]
  },
  {
    "slug": "advanced_spikes_wide_road_setup",
    "answer_mode": "single_choice",
    "category": "identify_error",
    "difficulty": "advanced",
    "prompt": "Con autorización de un supervisor, un deputy coloca varias bandas de pinchos para cubrir por completo una avenida y avisa por radio cuando el sospechoso ya está encima. ¿Cuál es el problema principal?",
    "explanation": "Cada miembro solo puede colocar una banda a la vez y debe comunicar su ubicación con antelación suficiente para no perjudicar a otras patrullas.",
    "source_reference": "Manual LSSD · III. Procedimientos · Uso de banda de pinchos",
    "options": [
      {"text": "Usó varias bandas y comunicó el despliegue demasiado tarde", "correct": true},
      {"text": "El único error fue usar más de una banda; la comunicación tardía sigue siendo válida si la primaria alcanza a verla", "correct": false},
      {"text": "El único error fue avisar tarde; con autorización puede cubrirse una avenida completa usando varias bandas", "correct": false},
      {"text": "El problema fue elegir una avenida, porque incluso una sola banda solo puede desplegarse en carreteras", "correct": false}
    ]
  },
  {
    "slug": "advanced_pursuit_pit_combined_conditions",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un supervisor autoriza una maniobra PIT en una avenida urbana a alta velocidad. ¿Por qué la autorización no basta para ejecutarla?",
    "explanation": "El PIT requiere autorización, pero además debe realizarse en carretera y a baja velocidad. Las condiciones operativas siguen siendo obligatorias.",
    "source_reference": "Manual LSSD · III. Procedimientos · Tácticas ofensivas",
    "options": [
      {"text": "Porque también deben cumplirse el lugar y la velocidad permitidos", "correct": true},
      {"text": "Porque la autorización basta solo cuando la unidad primaria confirma por radio que la maniobra es segura", "correct": false},
      {"text": "Porque a baja velocidad podría ejecutarse en cualquier vía sin autorización adicional", "correct": false},
      {"text": "Porque en zona urbana debe reemplazarse por un encajonamiento autorizado al detenerse el tráfico", "correct": false}
    ]
  },
  {
    "slug": "advanced_pursuit_lost_visual_followup",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Una unidad pierde de vista al vehículo después de una persecución ya comunicada por el parche. ¿Qué respuesta integra correctamente el procedimiento?",
    "explanation": "Debe transmitir la última ubicación y los datos clave para la búsqueda y levantar una emisión de crimen; no necesita pedir de nuevo el parche ya activo.",
    "source_reference": "Manual LSSD · III. Procedimientos · Persecuciones; IV. Comunicación · Pérdida de observación",
    "options": [
      {"text": "Informar pérdida de observación, última ubicación y descripción, y emitir el crimen", "correct": true},
      {"text": "Publicar primero la emisión de crimen en Discord y esperar autorización del supervisor para informarla por radio", "correct": false},
      {"text": "Solicitar nuevamente 10-34 y el parche antes de entregar cualquier dato, aunque el parche de la persecución siga activo", "correct": false},
      {"text": "Informar 10-98 de la persecución y dejar que SCC reconstruya la emisión con los datos transmitidos anteriormente", "correct": false}
    ]
  },
  {
    "slug": "advanced_mutual_aid_requesting_agency_command",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "Otra agencia solicita colaboración directa de LSSD y ya mantiene el control de la escena. ¿Cómo debe integrarse la unidad que responde?",
    "explanation": "La asistencia es obligatoria ante la solicitud, pero el mando permanece con la agencia solicitante salvo transferencia expresa.",
    "source_reference": "Manual LSSD · III. Procedimientos · Mutual Aid",
    "options": [
      {"text": "Prestar asistencia bajo el mando existente, salvo que la agencia solicite transferir el control", "correct": true},
      {"text": "Asumir el control si el miembro de LSSD posee mayor rango que el encargado de la otra agencia", "correct": false},
      {"text": "Tomar el mando táctico de LSSD y dejar a la otra agencia únicamente la coordinación administrativa", "correct": false},
      {"text": "Esperar autorización de un supervisor propio antes de prestar cualquier asistencia ya solicitada", "correct": false}
    ]
  },
  {
    "slug": "advanced_mutual_aid_split_pursuit",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Dos ocupantes se separan en vehículos distintos y la cantidad de deputies no permite mantener al menos tres unidades en cada persecución. ¿Qué consideración contempla el manual?",
    "explanation": "La desfragmentación en dos persecuciones sin unidades suficientes es una hipótesis para solicitar Mutual Aid.",
    "source_reference": "Manual LSSD · III. Procedimientos · Mutual Aid",
    "options": [
      {"text": "Solicitar Mutual Aid para sostener ambas persecuciones con seguridad", "correct": true},
      {"text": "Asignar tres unidades al sospechoso de mayor riesgo y mantener una sola siguiendo al segundo hasta recibir refuerzos", "correct": false},
      {"text": "Concentrar las unidades en una persecución y dejar solo una emisión de crimen para la otra, sin solicitar colaboración", "correct": false},
      {"text": "Mantener ambas persecuciones con las unidades disponibles y pedir Mutual Aid únicamente si salen de la jurisdicción", "correct": false}
    ]
  },
  {
    "slug": "advanced_shooting_scene_priority_sequence",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Cesa un tiroteo y todavía hay incertidumbre sobre heridos y sospechosos. ¿Qué secuencia refleja mejor las prioridades del manual?",
    "explanation": "Se prioriza cobertura y avance coordinado; al finalizar, se contabiliza a los deputies, se asegura y revisa a los sospechosos y luego se solicitan servicios médicos.",
    "source_reference": "Manual LSSD · III. Procedimientos · Incidentes con disparos o tiroteos",
    "options": [
      {"text": "Mantener cobertura, contabilizar deputies, asegurar sospechosos y solicitar atención médica", "correct": true},
      {"text": "Solicitar atención médica al cesar los disparos y después avanzar para asegurar y revisar a los sospechosos", "correct": false},
      {"text": "Contabilizar deputies, preservar primero casquillos y armas, y luego asegurar a los sospechosos heridos", "correct": false},
      {"text": "Informar código 4 al cesar el fuego, dividir al equipo y completar después el conteo y la revisión", "correct": false}
    ]
  },
  {
    "slug": "advanced_injured_suspect_restraint_balance",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Las esposas interfieren con la atención de un sospechoso herido y hay personal suficiente para asegurarlo con medios alternativos. ¿Qué criterio debe guiar la decisión?",
    "explanation": "El manual no impone una respuesta automática: exige valorar atención, seguridad y alternativas de sujeción o personal suficiente.",
    "source_reference": "Manual LSSD · III. Procedimientos · Personas heridas",
    "options": [
      {"text": "Balancear la atención médica con la seguridad y sustituir las esposas si las alternativas aseguran la escena", "correct": true},
      {"text": "Retirar las esposas cuando interfieran con la atención, trasladando al personal médico la responsabilidad de seguridad durante el tratamiento", "correct": false},
      {"text": "Mantenerlas en toda circunstancia hasta llegar al centro médico, aunque impidan el tratamiento", "correct": false},
      {"text": "Retirarlas solo si el sospechoso está inconsciente o no presenta pulso, sin considerar otros medios de seguridad", "correct": false}
    ]
  },
  {
    "slug": "advanced_traffic_passenger_identification",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "En una parada por una luz averiada, el conductor coopera y no hay indicios adicionales. Un pasajero se niega a identificarse. ¿Qué corresponde según el manual?",
    "explanation": "Puede pedirse identificación al pasajero, pero no está obligado a entregarla sin sospecha razonable de participación delictiva.",
    "source_reference": "Manual LSSD · III. Procedimientos · Parada de tráfico",
    "options": [
      {"text": "No exigirla solo por ser pasajero si no existe sospecha razonable", "correct": true},
      {"text": "Exigirla porque la detención válida del vehículo extiende el deber de identificarse a todos sus ocupantes", "correct": false},
      {"text": "Ordenarle bajar del vehículo y, una vez fuera, exigir la identificación aunque no existan nuevos indicios", "correct": false},
      {"text": "Retenerlo hasta finalizar todas las comprobaciones del conductor y usar su negativa como sospecha suficiente", "correct": false}
    ]
  },
  {
    "slug": "advanced_traffic_search_unrelated_warrant",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "Durante una parada se arresta al conductor por una orden pendiente ajena al vehículo. No hay consentimiento ni indicios de evidencia en el automóvil. ¿Qué fundamento es insuficiente para remolcarlo y registrarlo buscando otros delitos?",
    "explanation": "El manual impide usar un remolque como pretexto: el vehículo debe ser elemento del delito o existir otro fundamento válido de registro.",
    "source_reference": "Manual LSSD · III. Procedimientos · Parada de tráfico",
    "options": [
      {"text": "La sola orden ajena al vehículo y el deseo de buscar nueva evidencia", "correct": true},
      {"text": "Una orden de registro vigente que identifique expresamente ese vehículo", "correct": false},
      {"text": "El consentimiento voluntario y específico de quien puede autorizar el registro", "correct": false},
      {"text": "Causa probable vinculada a evidencia concreta que razonablemente estaría dentro del vehículo", "correct": false}
    ]
  },
  {
    "slug": "advanced_felony_stop_team_coordination",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "En una parada de alto riesgo con varios ocupantes, ¿qué organización reduce mejor el fuego cruzado y evita que el registro se adelante al arresto?",
    "explanation": "Las unidades se distribuyen a los lados, mantienen distancia y forman equipos: uno arresta a cada ocupante por orden y otro avanza al vehículo una vez asegurados.",
    "source_reference": "Manual LSSD · III. Procedimientos · Parada de tráfico forzosa",
    "options": [
      {"text": "Separar equipo de arresto y equipo de revisión, avanzando este último tras asegurar a los ocupantes", "correct": true},
      {"text": "Mantener separados ambos equipos, pero hacer avanzar al de revisión apenas el primer ocupante quede esposado", "correct": false},
      {"text": "Hacer salir simultáneamente a los ocupantes y asignar uno a cada deputy para reducir el tiempo de exposición", "correct": false},
      {"text": "Usar un solo equipo para arrestar y revisar por turnos, mientras el resto conserva cobertura desde el centro", "correct": false}
    ]
  },
  {
    "slug": "advanced_citation_refusal_business_hours",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "En horario hábil, un conductor sigue negándose a firmar una citación después de recibir la explicación y de que un supervisor intentara mediar. Hay captura de su negativa. ¿Qué sigue?",
    "explanation": "Debe arrestarse bajo CV-40302, incluir la infracción inicial y trasladarlo a la corte para fijar y firmar la citación, tras lo cual queda en libertad.",
    "source_reference": "Manual LSSD · III. Procedimientos · Negación a firmar citación",
    "options": [
      {"text": "Procesar CV-40302 con la infracción inicial y trasladarlo a la corte", "correct": true},
      {"text": "Procesar CV-40302 con la infracción inicial, pero trasladarlo a la estación para el arresto ordinario", "correct": false},
      {"text": "Permitir que el supervisor firme como testigo de la negativa, aplicar la citación y dejarlo en libertad", "correct": false},
      {"text": "Procesar CV-40307 y usar la corte solo si posteriormente acepta pagar una fianza", "correct": false}
    ]
  },
  {
    "slug": "advanced_barricade_risk_resource_choice",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Tras una larga persecución, un sospechoso posiblemente armado se encierra en un vehículo y se niega a salir. ¿Qué respuesta se ajusta mejor al nivel de riesgo?",
    "explanation": "Es un atrincheramiento de alto riesgo: corresponde perímetro, control de civiles y solicitud de SEB o, si no está disponible, D Platoon/SWAT.",
    "source_reference": "Manual LSSD · III. Procedimientos · Atrincheramientos",
    "options": [
      {"text": "Formar perímetro y solicitar el recurso táctico especializado", "correct": true},
      {"text": "Mantener contención y negociación con unidades ordinarias hasta confirmar visualmente el arma antes de solicitar SEB", "correct": false},
      {"text": "Formar un equipo de extracción con deputies ordinarios porque el vehículo detenido reduce el incidente a bajo riesgo", "correct": false},
      {"text": "Solicitar SEB, pero hacer que la unidad primaria abra el vehículo para impedir que el sospechoso gane más ventaja", "correct": false}
    ]
  },
  {
    "slug": "advanced_disturbance_group_control",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Varias personas participan en una pelea grupal y los deputies presentes son pocos. ¿Qué plan coincide mejor con el manual?",
    "explanation": "Se debe pedir asistencia, tomar distancia, mantener organización y concentrarse en un sospechoso sin separarse ni usar táser o escopeta de goma para detener la pelea.",
    "source_reference": "Manual LSSD · III. Procedimientos · Peleas y disturbios",
    "options": [
      {"text": "Pedir apoyo, conservar cohesión y reducir de forma coordinada sin menos letales prohibidos", "correct": true},
      {"text": "Pedir apoyo y dividirse en parejas para controlar simultáneamente a distintos participantes antes de definir un mando", "correct": false},
      {"text": "Mantener cohesión, pero usar beanbag contra el participante más agresivo después de una advertencia", "correct": false},
      {"text": "Tomar distancia y esperar instrucciones de un supervisor antes de intervenir, aunque continúen las agresiones", "correct": false}
    ]
  },
  {
    "slug": "advanced_risking_suspect_rescue_assignment",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un civil desarmado cae en una zona rocosa y pide ayuda. No existe una amenaza criminal activa. ¿Qué recurso debe priorizarse?",
    "explanation": "Para una persona desarmada o civil que solicita ayuda corresponde notificar al departamento de bomberos para el rescate, o al SEB si bomberos no está disponible.",
    "source_reference": "Manual LSSD · III. Procedimientos · Sospechosos que se ponen en riesgo",
    "options": [
      {"text": "Solicitar al departamento de bomberos y mantener una contención segura", "correct": true},
      {"text": "Solicitar primero SEB por tratarse de terreno de difícil acceso y derivar a bomberos solo si se descarta riesgo policial", "correct": false},
      {"text": "Mantener seguimiento con una unidad aérea hasta que SEB pueda efectuar el rescate, aunque se trate de un civil desarmado", "correct": false},
      {"text": "Formar un equipo de deputies con medios disponibles y reservar a bomberos para cuando existan lesiones confirmadas", "correct": false}
    ]
  },
  {
    "slug": "advanced_arrest_notification_after_danger",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "Un sospechoso sorprendido cometiendo un delito grave huye, es reducido y pregunta por qué lo arrestan mientras aún existe peligro en la escena. ¿Cuándo corresponde explicárselo?",
    "explanation": "La explicación puede diferirse durante la reducción o el peligro, pero debe darse cuando la situación esté calmada y el sospechoso se encuentre seguro.",
    "source_reference": "Manual LSSD · III. Procedimientos · Arrestos",
    "options": [
      {"text": "Cuando esté esposado y la situación permita responder sin comprometer la seguridad", "correct": true},
      {"text": "Una vez trasladado a las holding cells, para evitar discutir la causa mientras permanece en la escena", "correct": false},
      {"text": "Inmediatamente después de reducirlo y antes de colocar las esposas, aunque el equipo siga controlando el peligro", "correct": false},
      {"text": "Solo si vuelve a preguntar después del traslado, porque la flagrancia elimina la obligación inicial", "correct": false}
    ]
  },
  {
    "slug": "advanced_arrest_processing_traceability",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Después de un arresto se toman mugshots y se incauta un arma, pero el deputy guarda la bolsa en el maletero y redacta el reporte sin su número. ¿Qué combinación corrige la trazabilidad?",
    "explanation": "La evidencia debe etiquetarse y guardarse en el almacén de evidencia; el reporte debe incluir el número de bolsa y adjuntar los mugshots y cargos correspondientes.",
    "source_reference": "Manual LSSD · III. Procedimientos · Arrestos y evidencias; VI. Guía para escribir reportes",
    "options": [
      {"text": "Etiquetar y almacenar la bolsa, registrar su número y adjuntar los recursos del arresto", "correct": true},
      {"text": "Etiquetar la bolsa y anotar su número, manteniéndola temporalmente en el maletero hasta cerrar el turno", "correct": false},
      {"text": "Almacenar la bolsa y adjuntar las fotografías, dejando el número únicamente en el registro interno de evidencias", "correct": false},
      {"text": "Registrar el número de bolsa y los cargos en el reporte, incorporando los mugshots solo si los solicita un revisor", "correct": false}
    ]
  },
  {
    "slug": "advanced_evidence_scene_without_suspect",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "En una escena de tiroteo sin sospechoso identificado se recogen casquillos y una muestra de sangre. ¿Cómo debe adaptarse el etiquetado?",
    "explanation": "Cuando no hay sospechoso, la etiqueta se construye con el hecho, lugar y fecha, y la evidencia se almacena para su investigación posterior.",
    "source_reference": "Manual LSSD · III. Procedimientos · Evidencias",
    "options": [
      {"text": "Usar HECHO, LUGAR - FECHA y guardar las bolsas en evidencias", "correct": true},
      {"text": "Usar SOSPECHOSO DESCONOCIDO - FECHA y actualizar la etiqueta cuando exista una identificación", "correct": false},
      {"text": "Usar el número del incidente y la fecha, omitiendo el lugar para no asociar prematuramente la evidencia", "correct": false},
      {"text": "Usar el nombre del deputy que la recogió y la fecha, dejando el hecho y el lugar solo en el reporte", "correct": false}
    ]
  },
  {
    "slug": "advanced_evidence_storage_consequence",
    "answer_mode": "single_choice",
    "category": "identify_error",
    "difficulty": "advanced",
    "prompt": "Una evidencia fue empaquetada y etiquetada correctamente, pero queda en el maletero de la patrulla hasta el final del turno. ¿Cómo debe evaluarse?",
    "explanation": "El empaquetado no reemplaza el almacenamiento obligatorio; conservar evidencia en esos lugares constituye una falta grave.",
    "source_reference": "Manual LSSD · III. Procedimientos · Evidencias",
    "options": [
      {"text": "Sigue siendo una gestión incorrecta: debe almacenarse mediante /evidencias guardar", "correct": true},
      {"text": "Es válido si el maletero permanece cerrado y la bolsa se entrega antes de abandonar el servicio", "correct": false},
      {"text": "Es válido cuando la ubicación temporal y el número de bolsa quedan indicados en el reporte", "correct": false},
      {"text": "Solo constituye una falta si otro miembro utiliza la patrulla antes de que la evidencia sea retirada", "correct": false}
    ]
  },
  {
    "slug": "advanced_warrant_identity_evidence_chain",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "Se busca una orden contra una persona fotografiada durante una fuga. ¿Qué combinación permite fundamentar mejor su identificación según el manual?",
    "explanation": "La identificación puede apoyarse en documentación verdadera previa o en comparar el rostro con licencias o arrestos anteriores; además se requieren pruebas gráficas o registros fiables del hecho.",
    "source_reference": "Manual LSSD · III. Procedimientos · Órdenes de arresto y allanamiento",
    "options": [
      {"text": "Vincular la fotografía con documentación, DMV o arrestos previos y aportar pruebas del hecho", "correct": true},
      {"text": "Vincular la fotografía con el reconocimiento de otro deputy y respaldar el hecho únicamente con el chatlog", "correct": false},
      {"text": "Vincular el rostro con una licencia o arresto previo, aunque no exista una prueba fiable del delito que sustenta la orden", "correct": false},
      {"text": "Aportar pruebas gráficas completas del delito y usar el alias entregado por terceros como identificación suficiente", "correct": false}
    ]
  },
  {
    "slug": "advanced_search_warrant_weak_basis",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "Una propiedad pertenece a un miembro de una pandilla y aparecen sus huellas en un arma, pero no existe investigación adicional que vincule el inmueble con un delito. ¿Qué evaluación corresponde?",
    "explanation": "Ni la pertenencia a una organización ni las huellas aisladas bastan. Debe existir investigación y evidencia cruzada que fundamente la causa probable sobre la propiedad.",
    "source_reference": "Manual LSSD · III. Procedimientos · Allanamientos",
    "options": [
      {"text": "La base es insuficiente sin una vinculación investigativa adicional", "correct": true},
      {"text": "La base se vuelve suficiente si un supervisor certifica la pertenencia del sujeto a la pandilla", "correct": false},
      {"text": "La combinación de huellas y una denuncia anónima permite solicitar la orden aunque no haya pruebas cruzadas", "correct": false},
      {"text": "La base es suficiente si el propietario tiene un arresto previo por un delito parecido, aunque no se vincule al inmueble", "correct": false}
    ]
  },
  {
    "slug": "advanced_phone_search_consent_denied",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "Hay fundamentos para creer que un sospechoso usó su teléfono en un delito, pero niega el consentimiento para revisarlo. ¿Cuál es el siguiente paso correcto?",
    "explanation": "Debe redactarse un reporte de incidente que solicite una orden de registro, notificar a un líder y esperar que la orden sea firmada antes de revisar.",
    "source_reference": "Manual LSSD · III. Procedimientos · Revisión de teléfono celular",
    "options": [
      {"text": "Solicitar una orden mediante el reporte de incidente y esperar su firma", "correct": true},
      {"text": "Retener el teléfono y revisar solo metadatos básicos mientras se tramita la orden", "correct": false},
      {"text": "Solicitar autorización verbal a un líder para revisar el dispositivo y formalizar la orden después", "correct": false},
      {"text": "Pedir las credenciales para preservar la información, sin abrir aplicaciones hasta que exista una orden", "correct": false}
    ]
  },
  {
    "slug": "advanced_undocumented_traffic_stop_scope",
    "answer_mode": "single_choice",
    "category": "legal",
    "difficulty": "advanced",
    "prompt": "Durante una parada ordinaria, el conductor no presenta licencia y comenta que nació fuera del país. ¿Cómo debe mantenerse el alcance de la intervención?",
    "explanation": "El manual prohíbe preguntar por estatus migratorio durante la parada y exige concentrarse en la infracción que la originó.",
    "source_reference": "Manual LSSD · III. Procedimientos · Sospechosos indocumentados",
    "options": [
      {"text": "Centrado en la infracción y la documentación de conducción, sin investigar su estatus migratorio", "correct": true},
      {"text": "Puede preguntarse por su estatus después de emitir la citación, porque ya no alteraría el motivo inicial de la parada", "correct": false},
      {"text": "Debe retenerse brevemente al conductor para verificar residencia antes de decidir si puede continuar", "correct": false},
      {"text": "Debe iniciarse el procedimiento de deportación por no presentar licencia, aunque no exista un arresto por delito grave", "correct": false}
    ]
  },
  {
    "slug": "advanced_impound_third_party_vehicle",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un vehículo ajeno al sospechoso fue utilizado en una persecución y debe ser embargado. ¿Qué ajuste distingue este caso?",
    "explanation": "El vehículo puede embargarse por haber intervenido en la persecución, pero si no pertenece al sospechoso el embargo se realiza sin multa.",
    "source_reference": "Manual LSSD · III. Procedimientos · Embargo de vehículos",
    "options": [
      {"text": "Embargar sin multa y documentar el embargo", "correct": true},
      {"text": "Embargar con multa y permitir que el dueño solicite después su devolución demostrando que no participó", "correct": false},
      {"text": "Entregarlo al dueño registrado sin embargo, pero conservar fotografías y matrícula para el expediente", "correct": false},
      {"text": "Embargar sin multa, omitiendo el registro en el subforo porque el propietario no es el sospechoso", "correct": false}
    ]
  },
  {
    "slug": "advanced_station_transfer_escalation",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "Un deputy solicita trasladarse de estación. La estación de origen no tiene Unit Commander ni Operations Lieutenant, pero sí Operations Sergeant; la estación destino tiene Captain. ¿Qué aprobaciones necesita?",
    "explanation": "En origen se sigue la cadena hasta Operations Sergeant; además se requiere aprobación de un punto autorizado en destino, en este caso su Unit Commander/Captain.",
    "source_reference": "Manual LSSD · III. Procedimientos · Traslado de estación",
    "options": [
      {"text": "Operations Sergeant de origen y Captain de destino", "correct": true},
      {"text": "Operations Sergeant de origen; el Captain de destino solo debe ser notificado una vez ejecutado el traslado", "correct": false},
      {"text": "Captain de destino y Executive Staff; el Operations Sergeant de origen participa solo de forma consultiva", "correct": false},
      {"text": "Operations Sergeant de origen y Operations Sergeant de destino, aunque este último tenga un Captain disponible", "correct": false}
    ]
  },
  {
    "slug": "advanced_deputy_collision_scene_control",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Una patrulla y un civil chocan. No hay heridos graves y ambos vehículos aún pueden moverse. ¿Qué combinación de medidas conserva correctamente la escena?",
    "explanation": "Debe solicitarse una unidad Tom y, al estar involucrado un deputy, también un supervisor; los vehículos no se mueven hasta que un investigador de tráfico o supervisor lo ordene.",
    "source_reference": "Manual LSSD · III. Procedimientos · Accidentes de tráfico",
    "options": [
      {"text": "Solicitar Tom y supervisor, y mantener los vehículos hasta autorización", "correct": true},
      {"text": "Fotografiar y mover los vehículos para despejar la vía, y después solicitar Tom; el supervisor no es necesario sin heridos", "correct": false},
      {"text": "Solicitar supervisor y conservar la escena; Tom se requiere únicamente si los conductores discrepan sobre la responsabilidad", "correct": false},
      {"text": "Solicitar una grúa para retirar los vehículos y permitir que Tom realice la investigación en el depósito", "correct": false}
    ]
  },
  {
    "slug": "advanced_medical_refusal_after_force",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un detenido recibió un dardo de táser, dice sentirse bien y se niega a recibir tratamiento. ¿Qué debe hacer el deputy?",
    "explanation": "El impacto de un proyectil especializado exige evaluación médica. La negativa no evita el traslado; debe comunicarse personalmente al personal médico.",
    "source_reference": "Manual LSSD · III. Procedimientos · Traslado en ambulancia",
    "options": [
      {"text": "Trasladarlo para evaluación e informar al personal médico de su negativa", "correct": true},
      {"text": "Documentar la negativa con un segundo deputy y continuar el traslado a custodia si no presenta lesiones visibles", "correct": false},
      {"text": "Solicitar que una unidad médica lo evalúe en escena y aceptar la negativa escrita sin trasladarlo", "correct": false},
      {"text": "Consultar a un supervisor, quien puede dispensar el examen si el detenido está consciente y orientado", "correct": false}
    ]
  },
  {
    "slug": "advanced_k9_reasonable_need_limits",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un sospechoso huyó hacia una zona amplia y existe una descripción parcial, pero no un punto exacto. ¿Cómo debe entenderse el apoyo K-9?",
    "explanation": "Puede solicitarse por una necesidad operativa razonable, pero el perro no garantiza resultados ni permite encontrar automáticamente a cualquier persona ignorando el entorno.",
    "source_reference": "Manual LSSD · III. Procedimientos · Pedido de unidad K9",
    "options": [
      {"text": "Como apoyo de búsqueda sujeto a indicios y limitaciones del entorno", "correct": true},
      {"text": "Como recurso que permite reducir el perímetro, porque el rastro del K-9 sustituye parte de la contención", "correct": false},
      {"text": "Como búsqueda dirigida por la última dirección conocida, considerando un resultado negativo como descarte del área", "correct": false},
      {"text": "Como recurso reservado para después de que SEB determine un punto probable de ocultamiento", "correct": false}
    ]
  },
  {
    "slug": "advanced_seb_mandatory_hostage_incident",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Una toma de rehenes comienza con un solo sospechoso y todavía no se confirma el tipo de arma. ¿Qué implica para la activación de SEB?",
    "explanation": "La toma de rehenes está incluida por sí misma entre los incidentes que requieren obligatoriamente solicitar SEB.",
    "source_reference": "Manual LSSD · III. Procedimientos · Solicitud de SEB para situaciones de alto riesgo",
    "options": [
      {"text": "Debe solicitarse SEB aunque haya un solo sospechoso", "correct": true},
      {"text": "Debe solicitarse cuando se confirme que el sospechoso está armado o que existen al menos dos responsables", "correct": false},
      {"text": "El IC puede reservar la solicitud hasta que la negociación falle, porque aún no hay un ataque activo", "correct": false},
      {"text": "Debe solicitarse solo si la ventaja del sospechoso impide que las unidades ordinarias formen un perímetro completo", "correct": false}
    ]
  },
  {
    "slug": "advanced_seb_tactical_vs_incident_command",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "SEB llega a un incidente que ya posee Incident Commander. Su integrante de mayor rango asume la parte especializada. ¿Cómo se distribuye el mando?",
    "explanation": "El manual delimita el mando táctico a la parte especializada, mientras el manejo general del incidente conserva sus propias responsabilidades.",
    "source_reference": "Manual LSSD · III. Procedimientos · Solicitud de SEB para situaciones de alto riesgo",
    "options": [
      {"text": "SEB dirige el componente táctico y el IC conserva el manejo general del incidente", "correct": true},
      {"text": "El miembro de mayor rango de SEB reemplaza al IC y asume todas las decisiones operativas y administrativas", "correct": false},
      {"text": "El IC conserva también el mando táctico; SEB únicamente recomienda técnicas y recursos", "correct": false},
      {"text": "El mando táctico corresponde al miembro de mayor rango departamental, aunque no pertenezca a SEB", "correct": false}
    ]
  },
  {
    "slug": "advanced_parole_visit_trainee_team",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un trainee y otro trainee quieren visitar a una persona en libertad condicional. No los acompaña ningún deputy que haya completado FTP. ¿Qué falta?",
    "explanation": "Las visitas requieren al menos dos deputies y los trainees solo pueden participar acompañados por al menos un deputy que haya completado el Field Training Program.",
    "source_reference": "Manual LSSD · III. Procedimientos · Protocolos de libertad condicional",
    "options": [
      {"text": "La presencia de un deputy que haya completado FTP", "correct": true},
      {"text": "La autorización remota de un supervisor que pueda monitorear la visita, aunque no los acompañe", "correct": false},
      {"text": "Un tercer trainee, porque el número de integrantes compensa la falta de uno con FTP completado", "correct": false},
      {"text": "Un segundo deputy de cualquier fase del FTP, si uno de los trainees posee mayor antigüedad", "correct": false}
    ]
  },
  {
    "slug": "advanced_parole_violation_charge_source",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "Durante una visita se confirma un incumplimiento de libertad condicional y corresponde arrestar. ¿De dónde deben obtenerse los cargos aplicables?",
    "explanation": "El arresto por incumplimiento usa los mismos cargos del arresto que originó la libertad condicional, es decir, el último reporte de arresto del personaje.",
    "source_reference": "Manual LSSD · III. Procedimientos · Protocolos de libertad condicional",
    "options": [
      {"text": "Del último reporte de arresto que originó su libertad condicional", "correct": true},
      {"text": "De las reglas de parole incumplidas, seleccionando cargos nuevos que describan la conducta observada en la visita", "correct": false},
      {"text": "Del último reporte y de cualquier delito adicional asociado al incumplimiento, aunque no se haya cometido durante la visita", "correct": false},
      {"text": "De la equivalencia que determine el supervisor entre la regla vulnerada y el código penal vigente", "correct": false}
    ]
  },
  {
    "slug": "advanced_tactical_alert_resource_permissions",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Se declara una alerta táctica por una seguidilla de tiroteos. ¿Cuál conjunto de cambios operativos puede habilitarse sin convertir la alerta en autorización irrestricta?",
    "explanation": "La alerta permite medidas controladas como patrullaje de a dos, casco y chaleco, rifles urbanos, unidades aéreas, código 9 y SEB; sigue siendo una distribución dirigida de recursos.",
    "source_reference": "Manual LSSD · III. Procedimientos · Alertas tácticas",
    "options": [
      {"text": "Redistribución controlada, binomios y recursos tácticos expresamente autorizados", "correct": true},
      {"text": "Patrullaje en binomios, pero con elección individual de rifles y código 9 según el criterio de cada unidad", "correct": false},
      {"text": "Concentración de toda la fuerza de campo en la zona afectada, manteniendo patrullaje individual en el resto", "correct": false},
      {"text": "Activación limitada a la estación convocada, dejando SEB y Aero fuera hasta que el IC de un incidente los solicite", "correct": false}
    ]
  },
  {
    "slug": "advanced_aircraft_dodo_authorization_mission",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un deputy sin asignación especializada recibe autorización de un supervisor para usar el Dodo. Quiere emplearlo en patrullaje general. ¿Qué parte del plan sigue siendo incorrecta?",
    "explanation": "Un supervisor puede autorizar al operador, pero el Dodo conserva misiones limitadas a vigilancia aérea y búsqueda y rescate, usando callsign Surveillance.",
    "source_reference": "Manual LSSD · III. Procedimientos · Despliegue de unidades aéreas",
    "options": [
      {"text": "La misión de patrullaje general no está dentro de los usos permitidos del Dodo", "correct": true},
      {"text": "La autorización debe indicar expresamente patrullaje general; sin esa precisión solo habilita al operador, no la misión", "correct": false},
      {"text": "La misión es válida, pero debería utilizar una callsign Air ordinaria en vez de Surveillance", "correct": false},
      {"text": "La vigilancia y búsqueda son válidas únicamente cuando SEB solicita la aeronave, no por decisión de un supervisor", "correct": false}
    ]
  },
  {
    "slug": "advanced_motorcycle_ftp_authorization",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Un trainee solicita a un supervisor desplegar una HPV1000 para una persecución de motocicletas. El supervisor acepta. ¿Qué regla impide el despliegue?",
    "explanation": "Las motocicletas están excluidas para quienes siguen en el Field Training Program; la autorización del supervisor no elimina esa restricción.",
    "source_reference": "Manual LSSD · III. Procedimientos · Despliegue de unidades motorizadas",
    "options": [
      {"text": "Los miembros en FTP no pueden desplegar la motocicleta", "correct": true},
      {"text": "Puede desplegarla con autorización si limita su participación a la persecución y no al patrullaje general", "correct": false},
      {"text": "Puede desplegarla si su FTO participa en otra unidad y mantiene supervisión por radio", "correct": false},
      {"text": "La restricción de FTP aplica al patrullaje, pero no a una respuesta específica relacionada con motocicletas", "correct": false}
    ]
  },
  {
    "slug": "advanced_teamspeak_whisper_sequence",
    "answer_mode": "single_choice",
    "category": "procedure",
    "difficulty": "advanced",
    "prompt": "Un deputy de servicio necesita enviar un whisper por un incidente mientras permanece en su canal de patrulla muteado. ¿Qué debe hacer primero?",
    "explanation": "Los whispers deben enviarse desde un canal A-TAC, por lo que el deputy debe moverse allí antes de transmitir.",
    "source_reference": "Manual LSSD · III. Procedimientos · Reglas del TeamSpeak",
    "options": [
      {"text": "Moverse a un canal A-TAC y enviar el whisper desde allí", "correct": true},
      {"text": "Enviar el whisper desde patrulla sin hablar en el canal, porque la restricción solo afecta la conversación abierta", "correct": false},
      {"text": "Pedir al receptor que se mueva a A-TAC; el emisor puede permanecer en su canal si usa whisper", "correct": false},
      {"text": "Usar un canal operativo de Discord cuando A-TAC esté ocupado, manteniendo TeamSpeak solo como presencia", "correct": false}
    ]
  },
  {
    "slug": "advanced_radio_patch_support_assistance",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "advanced",
    "prompt": "Una unidad necesita presencia adicional de rutina, sin luces ni sirenas. Pide 10-33 y parche antes de solicitarla. ¿Qué concepto confundió?",
    "explanation": "El apoyo es rutinario y no requiere respuesta en código ni parche; el parche se asocia a asistencia de emergencia.",
    "source_reference": "Manual LSSD · IV. Comunicación · Conceptos y uso de parche",
    "options": [
      {"text": "Trató un pedido de apoyo como una solicitud de asistencia de emergencia", "correct": true},
      {"text": "El apoyo puede usar parche cuando requiere coordinar más de una estación, aunque no exista una emergencia", "correct": false},
      {"text": "La asistencia solo necesita parche si SCC no confirma haber recibido el pedido inicial", "correct": false},
      {"text": "El 10-33 convierte correctamente la solicitud en apoyo prioritario sin necesidad de responder en código", "correct": false}
    ]
  },
  {
    "slug": "advanced_radio_arrival_without_response",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "advanced",
    "prompt": "Una unidad llega a una escena sin haber informado previamente que respondía. ¿Qué transmisión evita usar incorrectamente el 10-97?",
    "explanation": "El 10-97 procede cuando ya se informó la respuesta. Si no existió esa comunicación previa, corresponde informar código 6 en la ubicación.",
    "source_reference": "Manual LSSD · IV. Comunicación · Respuestas al llegar a la escena",
    "options": [
      {"text": "Informar código 6 en la ubicación", "correct": true},
      {"text": "Informar 10-97, porque una asignación previa de SCC sustituye el anuncio de que la unidad estaba respondiendo", "correct": false},
      {"text": "Informar que está respondiendo y luego 10-97 en la misma transmisión, aunque ya se encuentre en escena", "correct": false},
      {"text": "Informar 10-22 y a continuación 10-97, corrigiendo así la ausencia de una respuesta previa", "correct": false}
    ]
  },
  {
    "slug": "advanced_radio_custody_felony_timing",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "advanced",
    "prompt": "Un sospechoso de felony está esposado, pero sigue junto al vehículo, alterado y con riesgo razonable de lucha. ¿Por qué conviene esperar antes de informar 10-15?",
    "explanation": "En delitos graves el 10-15 se informa cuando, además de estar esposado, el sospechoso está asegurado en una unidad o el riesgo de fuga o lucha es mínimo.",
    "source_reference": "Manual LSSD · IV. Comunicación · Sospechoso en custodia",
    "options": [
      {"text": "Porque la custodia todavía no está suficientemente asegurada para ese anuncio", "correct": true},
      {"text": "Porque las esposas bastan solo después de terminar la revisión corporal; el asiento trasero es recomendable, no determinante", "correct": false},
      {"text": "Porque requiere que un supervisor confirme primero que la escena está código 4, aunque el sospechoso ya esté asegurado", "correct": false},
      {"text": "Porque en un felony el 10-15 se informa al concluir toda la parada forzosa, independientemente del riesgo individual", "correct": false}
    ]
  },
  {
    "slug": "advanced_radio_crime_broadcast_sequence",
    "answer_mode": "single_choice",
    "category": "radio_code",
    "difficulty": "advanced",
    "prompt": "Después de perder a un sospechoso, una unidad debe emitir una descripción extensa por radio. ¿Qué orden favorece que el resto pueda escucharla y actuar?",
    "explanation": "La emisión comienza con 10-34 y el pedido de parche, seguido en líneas separadas por hecho, tiempo y lugar, descripción, dirección de huida y arma.",
    "source_reference": "Manual LSSD · IV. Comunicación · Emisión de crimen",
    "options": [
      {"text": "Abrir con 10-34/parche y luego transmitir los datos clave en líneas separadas", "correct": true},
      {"text": "Abrir con 10-34 y transmitir toda la información en una sola emisión para reducir el tiempo que la frecuencia queda ocupada", "correct": false},
      {"text": "Entregar primero descripción y hecho, usar 10-34 antes de la dirección de huida y cerrar solicitando el parche", "correct": false},
      {"text": "Pedir el parche mediante 10-33 y resumir hecho, descripción y dirección en una sola línea de alta prioridad", "correct": false}
    ]
  },
  {
    "slug": "advanced_radio_pursuit_information_priority",
    "answer_mode": "single_choice",
    "category": "scenario",
    "difficulty": "advanced",
    "prompt": "Al iniciar una persecución, la unidad informa color y modelo, pero omite dirección, velocidad, ocupantes, tráfico y si existen armas. ¿Cuál es el efecto operativo de esas omisiones?",
    "explanation": "Esos datos permiten a las unidades responder, anticipar el recorrido y valorar riesgos; la comunicación debe integrar identificación, ubicación, dirección, velocidad, motivo, ocupantes, tráfico y amenazas.",
    "source_reference": "Manual LSSD · IV. Comunicación · Persecución",
    "options": [
      {"text": "Impide coordinar una respuesta segura y situacionalmente informada", "correct": true},
      {"text": "Es suficiente para iniciar; ubicación y dirección pueden entregarse después por la secundaria mientras la primaria conduce", "correct": false},
      {"text": "Permite coordinar si se agregan motivo y armas; velocidad, tráfico y ocupantes son datos complementarios", "correct": false},
      {"text": "Debe priorizarse matrícula y descripción del vehículo, dejando el motivo para cuando SCC confirme la identidad del dueño", "correct": false}
    ]
  },
  {
    "slug": "advanced_chain_ic_and_supervision",
    "answer_mode": "single_choice",
    "category": "general",
    "difficulty": "advanced",
    "prompt": "Un Deputy Sheriff inició una situación y conserva el manejo como IC. Llega un FTO y se necesita autorizar una maniobra PIT, sin Supervisory Staff presente. ¿Cómo se reparten las funciones?",
    "explanation": "El IC conserva el manejo del incidente, mientras el FTO ocupa la responsabilidad jerárquica de supervisar y autorizar la maniobra ante la ausencia de Supervisory Staff.",
    "source_reference": "Manual LSSD · V. Organización · Cadena de mando en situaciones",
    "options": [
      {"text": "El IC mantiene el manejo y el FTO ejerce la supervisión requerida", "correct": true},
      {"text": "El FTO asume el manejo por jerarquía y el Deputy permanece como unidad primaria para transmitir", "correct": false},
      {"text": "El Deputy conserva manejo y autorización por haber iniciado; el FTO solo puede recomendar la maniobra", "correct": false},
      {"text": "El FTO puede autorizar únicamente si el Deputy le transfiere formalmente el rol de Incident Commander", "correct": false}
    ]
  },
  {
    "slug": "advanced_report_integrated_completeness",
    "answer_mode": "single_choice",
    "category": "reports",
    "difficulty": "advanced",
    "prompt": "Un reporte explica claramente la detención y los cargos, pero omite quién tomó el manejo, las unidades que llegaron y los números de bolsas de evidencia incautada. ¿Cuál es la mejor evaluación?",
    "explanation": "Una narrativa comprensible no reemplaza los elementos obligatorios: debe identificar participantes y manejo, y mantener la referencia de la evidencia asociada.",
    "source_reference": "Manual LSSD · VI. Guía para escribir reportes",
    "options": [
      {"text": "La narrativa puede ser clara, pero el reporte sigue incompleto en coordinación y trazabilidad", "correct": true},
      {"text": "Está completo si los nombres aparecen en los adjuntos y los números de bolsa permanecen visibles en el almacén de evidencias", "correct": false},
      {"text": "La identificación del manejo y las unidades es opcional cuando la narrativa demuestra de forma suficiente los cargos", "correct": false},
      {"text": "Los números pueden omitirse si la evidencia se describe con precisión y se adjuntan grabaciones del procedimiento", "correct": false}
    ]
  }
]
$manual_quiz_questions$::jsonb
) AS payload(item);

DO $manual_quiz_validate$
DECLARE
    invalid_count integer;
BEGIN
    SELECT count(*)
    INTO invalid_count
    FROM manual_quiz_seed_items AS item
    WHERE item.slug !~ '^[a-z0-9]+(_[a-z0-9]+)*$'
       OR item.answer_mode NOT IN ('single_choice', 'true_false')
       OR item.category NOT IN (
           'general', 'radio_code', 'procedure', 'scenario', 'identify_error', 'legal',
           'reports'
       )
       OR item.difficulty NOT IN ('basic', 'intermediate', 'advanced')
       OR jsonb_typeof(item.options) <> 'array'
       OR jsonb_array_length(item.options) NOT BETWEEN 2 AND 10
       OR (
           SELECT count(*)
           FROM jsonb_array_elements(item.options) AS option
           WHERE (option->>'correct')::boolean
       ) <> 1
       OR (
           item.answer_mode = 'true_false'
           AND (
               jsonb_array_length(item.options) <> 2
               OR item.options->0->>'text' <> 'Verdadero'
               OR item.options->1->>'text' <> 'Falso'
           )
       );

    IF invalid_count > 0 THEN
        RAISE EXCEPTION 'el seed contiene % preguntas invalidas', invalid_count;
    END IF;
END
$manual_quiz_validate$;

CREATE TEMPORARY TABLE manual_quiz_seed_changed (
    question_id uuid PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO manual_quiz_seed_changed (question_id)
SELECT question.id
FROM manual_quiz_questions AS question
JOIN manual_quiz_seed_items AS seed
  ON seed.slug = question.slug
WHERE question.guild_id = :'guild_id'::bigint
  AND (
      ROW(
          question.answer_mode,
          question.category,
          question.difficulty,
          question.prompt,
          question.explanation,
          question.source_reference,
          question.status
      ) IS DISTINCT FROM ROW(
          seed.answer_mode,
          seed.category,
          seed.difficulty,
          seed.prompt,
          seed.explanation,
          seed.source_reference,
          'active'::text
      )
      OR COALESCE(
          (
              SELECT jsonb_agg(
                  jsonb_build_object(
                      'text', option.text,
                      'correct', option.is_correct
                  )
                  ORDER BY option.position
              )
              FROM manual_quiz_options AS option
              WHERE option.question_id = question.id
          ),
          '[]'::jsonb
      ) IS DISTINCT FROM seed.options
  );

INSERT INTO manual_quiz_questions (
    id,
    guild_id,
    slug,
    answer_mode,
    category,
    difficulty,
    prompt,
    explanation,
    source_reference,
    status,
    revision
)
SELECT
    md5(:'guild_id' || ':' || seed.slug)::uuid,
    :'guild_id'::bigint,
    seed.slug,
    seed.answer_mode,
    seed.category,
    seed.difficulty,
    seed.prompt,
    seed.explanation,
    seed.source_reference,
    'active',
    1
FROM manual_quiz_seed_items AS seed
ON CONFLICT (guild_id, slug) DO NOTHING;

UPDATE manual_quiz_questions AS question
SET answer_mode = seed.answer_mode,
    category = seed.category,
    difficulty = seed.difficulty,
    prompt = seed.prompt,
    explanation = seed.explanation,
    source_reference = seed.source_reference,
    status = 'active',
    revision = question.revision + 1,
    updated_at = now()
FROM manual_quiz_seed_items AS seed
JOIN manual_quiz_seed_changed AS changed
  ON true
WHERE question.guild_id = :'guild_id'::bigint
  AND question.slug = seed.slug
  AND changed.question_id = question.id;

DELETE FROM manual_quiz_options AS option
USING manual_quiz_questions AS question, manual_quiz_seed_items AS seed
WHERE option.question_id = question.id
  AND question.guild_id = :'guild_id'::bigint
  AND question.slug = seed.slug;

INSERT INTO manual_quiz_options (
    id,
    question_id,
    position,
    text,
    is_correct
)
SELECT
    md5(:'guild_id' || ':' || question.slug || ':' || (entry.ordinality - 1)::text)::uuid,
    question.id,
    (entry.ordinality - 1)::integer,
    entry.option->>'text',
    (entry.option->>'correct')::boolean
FROM manual_quiz_seed_items AS seed
JOIN manual_quiz_questions AS question
  ON question.guild_id = :'guild_id'::bigint
 AND question.slug = seed.slug
CROSS JOIN LATERAL jsonb_array_elements(seed.options)
    WITH ORDINALITY AS entry(option, ordinality);

SELECT
    count(*) AS active_questions,
    count(*) FILTER (WHERE answer_mode = 'single_choice') AS single_choice,
    count(*) FILTER (WHERE answer_mode = 'true_false') AS true_false
FROM manual_quiz_questions
WHERE guild_id = :'guild_id'::bigint
  AND status = 'active'
  AND slug IN (SELECT slug FROM manual_quiz_seed_items);

COMMIT;
