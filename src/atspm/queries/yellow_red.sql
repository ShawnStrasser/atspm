--Actuations on yellow and red
--Written in SQL for DuckDB. This is a jinja2 template, with variables inside curly braces.
--
--Each actuation in a whole cycle, as the seconds from that cycle's red begin, in the bin of that red begin.
--Red Offset is rounded to nearest half second. This was based on visual observation of the smoothness of rounding from 0.2 seconds to 1 second, seemed to look good
{% set red_offset = "ROUND(DATEDIFF('MILLISECOND', c.Red_Begin, l.TimeStamp)::float * 2 / 1000) / 2" %}
{% include 'red_offsets.sql' %}
{%- if min_red_offset is defined %}
WHERE Red_Offset >= {{ min_red_offset }}
{%- endif %}
