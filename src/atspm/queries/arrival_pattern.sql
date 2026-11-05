--Arrival Pattern
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--
--yellow_red for advance detectors. Each on event is moved forward to the stop bar as in arrival_on_green, with no
--spillback flag. Each arrival in a whole cycle, as the seconds from that cycle's red begin, in the bin of that red begin,
--rounded to the nearest offset_slice_seconds.
{% set arrival_pattern = True %}
{% set red_offset = "(ROUND(DATEDIFF('MILLISECOND', c.Red_Begin, l.TimeStamp) / (" ~ offset_slice_seconds ~ " * 1000.0)) * " ~ offset_slice_seconds ~ ")::float" %}
{% include 'red_offsets.sql' %}
