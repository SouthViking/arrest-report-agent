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
