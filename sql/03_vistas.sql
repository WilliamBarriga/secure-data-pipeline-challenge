-- Hueco: hora local sin Lectura valida, exista o no fila en el archivo.
-- Universo: Fronteras del maestro o vistas en consumos x meses con al menos una fila cargada x horas
-- calendario del mes en America/Bogota (UTC-5 fijo). Nada se imputa.
CREATE VIEW core.v_hueco AS
SELECT f.frontera_id, h.ts_hora
FROM (SELECT frontera_id FROM core.dim_frontera
      UNION SELECT frontera_id FROM core.fact_consumo_horario) f
CROSS JOIN (SELECT DISTINCT mes_local FROM core.fact_consumo_horario) m
CROSS JOIN LATERAL generate_series(
    m.mes_local::timestamp AT TIME ZONE 'America/Bogota',
    (m.mes_local + interval '1 month' - interval '1 hour') AT TIME ZONE 'America/Bogota',
    interval '1 hour') AS h(ts_hora)
WHERE NOT EXISTS (
    SELECT 1 FROM core.fact_consumo_horario c
    WHERE c.frontera_id = f.frontera_id AND c.ts_hora = h.ts_hora);

-- Unico punto de acceso del analista: sin PII (de dim_cliente solo cliente_id).
CREATE VIEW analytics.v_consumo_mensual WITH (security_barrier = true) AS
WITH medido AS (
    SELECT frontera_id, mes_local,
           sum(consumo_kwh) AS consumo_kwh,
           count(*)::integer AS horas_con_dato
    FROM core.fact_consumo_horario
    GROUP BY frontera_id, mes_local
), hueco AS (
    SELECT frontera_id,
           (date_trunc('month', ts_hora AT TIME ZONE 'America/Bogota'))::date AS mes_local,
           count(*)::integer AS horas_hueco
    FROM core.v_hueco
    GROUP BY 1, 2
), facturado AS (
    SELECT frontera_id, periodo AS mes_local,
           sum(consumo_facturado_kwh) AS consumo_facturado_kwh
    FROM core.fact_factura
    GROUP BY frontera_id, periodo
), base AS (
    SELECT frontera_id, mes_local FROM medido
    UNION
    SELECT frontera_id, mes_local FROM facturado
)
SELECT b.frontera_id,
       fr.cliente_id,
       to_char(b.mes_local, 'YYYY-MM') AS mes,
       m.consumo_kwh,
       COALESCE(m.horas_con_dato, 0) AS horas_con_dato,
       COALESCE(h.horas_hueco, 0) AS horas_hueco,
       fa.consumo_facturado_kwh,
       fa.consumo_facturado_kwh - m.consumo_kwh AS diferencia_kwh,
       CASE WHEN m.consumo_kwh IS NULL OR m.consumo_kwh = 0
                 OR fa.consumo_facturado_kwh IS NULL THEN NULL
            ELSE round((fa.consumo_facturado_kwh - m.consumo_kwh) / m.consumo_kwh * 100, 2)
       END AS diferencia_pct,
       fr.operador_red,
       fr.nivel_tension
FROM base b
LEFT JOIN core.dim_frontera fr USING (frontera_id)
LEFT JOIN medido m USING (frontera_id, mes_local)
LEFT JOIN hueco h USING (frontera_id, mes_local)
LEFT JOIN facturado fa USING (frontera_id, mes_local);
