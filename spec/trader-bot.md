# Trader-bot

## Objetivo
Diseñar un sistema de trading algorítmico basado en agentes de Inteligencia Artificial que permita identificar el mejor momento para comprar o vender mis acciones. 
Se deben separar responsabilidades usando una arquitectura multi-agente (un agente analiza noticias, otro mira gráficos técnicos y otro gestiona el riesgo).
Regla de oro inquebrantable: ningún LLM (como Claude) debe tener la última palabra ni el control directo sobre la ejecución de una orden de compra/venta sin un filtro algorítmico rígido y determinista (código Python puro). 

## 1. Arquitectura del Sistema (Multi-Agente)

Para una solución robusta en Python, la arquitectura ideal divide el problema en agentes especializados coordinados por un "Orquestador".

[Fuentes de Datos (APIs)] 
       │
       ▼
┌────────────────────────────────────────────────────────┐
│               ORQUESTADOR CENTRAL                      │
└──────┬───────────────────┬───────────────────┬─────────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Agente 1:   │    │  Agente 2:   │    │  Agente 3:   │
│ Analista     │    │ Analista     │    │ Sentimiento  │
│ Técnico      │    │ Fundamental  │    │ de Mercado   │
└──────┬───────┘    └──────┬───────┘    └──────┬───────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │ (Recomendaciones de trade)
                           ▼
┌────────────────────────────────────────────────────────┐
│            AGENTE 4: GESTOR DE RIESGO (Risk Warden)    │
│            *Aplica reglas matemáticas estrictas*       │
└──────────────────────────┬─────────────────────────────┘
                           │ (Orden Aprobada / Rechazada)
                           ▼
┌────────────────────────────────────────────────────────┐
│            MÓDULO DE EJECUCIÓN (Broker API)            │
└────────────────────────────────────────────────────────┘

Componentes Clave:

1. Orquestador Central: Recopila los datos del mercado y coordina las llamadas a los agentes. Recibe el veredicto final.
2. Agente Analista Técnico (Claude API): Recibe tablas de datos de precios históricos (Velocidad, RSI, MACD, Medias Móviles) y busca patrones visuales o de tendencia en formato de texto o JSON.
3. Agente Analista Fundamental (Claude API): Analiza los reportes trimestrales de ganancias (Earnings Reports) y métricas financieras (P/E ratio, deuda, crecimiento).
4. Agente de Sentimiento (Claude API): Escanea noticias recientes y redes sociales (X/Twitter, Reddit) sobre la acción para determinar si el ánimo del mercado es alcista o bajista.
5. Agente Gestor de Riesgo (Python Puro - Crítico): Modera las decisiones de los agentes de IA. Si la IA decide comprar pero rompe una regla de gestión de capital, la operación se cancela automáticamente.

## 2. Motor de Reglas de Oro (Protección y Ejecución)

Estas reglas deben programarse en Python de forma determinista (usando if/else numéricos), nunca dejando que la IA decida mediante lenguaje natural si se cumple o no la regla.

Reglas de Protección (Gestión de Riesgo)

• Stop-Loss Máximo Estricto (Tu protección X%): Cada operación debe abrirse con un stop-loss automático. Si una acción cae un X% (por ejemplo, 2% o 3%) desde el precio de compra, el broker vende inmediatamente de forma automática.
• Regla de Máxima Pérdida Diaria (Circuit Breaker): Si el portafolio total pierde más del Y% en un solo día (ej. 5%), el bot se apaga por completo y cancela todas las órdenes abiertas hasta el día siguiente.
• Tamaño de la Posición (Position Sizing): Ninguna operación individual puede arriesgar más del 1% al 2% del capital total del portafolio. Si tienes $10,000 USD, no arriesgues más de $200 USD por trade.

Reglas de Decisión (Compra y Venta)

• Regla de Confluencia de Agentes: Para que el Orquestador apruebe una compra, al menos el Agente Técnico y el Agente de Sentimiento deben emitir una señal de "COMPRA fuerte". Si están en conflicto, el bot se mantiene en efectivo (cash).
• Take-Profit Dinámico (Trailing Stop): Si la operación va ganando, el Stop-Loss se mueve hacia arriba automáticamente para asegurar ganancias en caso de una reversión repentina del mercado.

## 