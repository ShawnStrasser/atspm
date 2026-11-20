-- One stage of the arrival chain as a temp table, built once for a run that writes both arrival_on_green and what it
-- carries to the next run (arrival_on_green_carry.sql). Both then read the stages instead of building the chain twice.
-- Stages, in order: flags (detector_flags, the spillback flag), queue (queue_results, the queue model) and state
-- (with_signal_state). Each later stage reads the ones built before it, listed in shared_stages.
CREATE OR REPLACE TEMP TABLE _arrival_{{stage}} AS
WITH {% include 'arrival_phase_events.sql' %},

{% include 'arrival_projection.sql' %},

{% include 'arrival_hold.sql' %}

SELECT * FROM {% if stage == 'flags' %}detector_flags{% elif stage == 'queue' %}queue_results{% else %}with_signal_state{% endif %}
