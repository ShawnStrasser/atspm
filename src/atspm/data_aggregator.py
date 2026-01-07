import os
from jinja2 import Environment, FileSystemLoader

# Cache the Jinja2 environment at module level to avoid recreating it for every query
# This improves performance and reduces memory churn from repeated Environment instantiation
_template_dir = os.path.join(os.path.dirname(__file__), 'queries')
_jinja_env = Environment(loader=FileSystemLoader(_template_dir))

def render_query(query_name, **kwargs):
    # Get the template by name from the cached environment
    template = _jinja_env.get_template(f"{query_name}.sql")
    # Render the template with the provided keyword arguments
    return template.render(**kwargs)

def aggregate_data(conn, aggregation_name, to_sql, **kwargs):
    query = render_query(aggregation_name, **kwargs)

    # Option to remove incomplete data (natural join with DeviceId, TimeStamp columns)
    if aggregation_name not in ['has_data', 'unmatched_events', 'timeline', 'split_failures'] and kwargs['remove_incomplete']:
        # Add natural join with has_data table
        query = f"SELECT * FROM ({query}) main_query NATURAL JOIN has_data "

    # Split failures are different to allow for saving incomplete cycle data
    if aggregation_name == 'split_failures':
        if kwargs['remove_incomplete']:
            query += " CREATE TABLE split_failures AS SELECT * FROM sf_final NATURAL JOIN has_data; "
        else:
            query += " CREATE TABLE split_failures AS SELECT * FROM sf_final; "
    else:
        query = f"CREATE TABLE {aggregation_name} AS {query}; "

    # For timeline aggregation, get unmatched rows (EndTime is null) and put them into table 'unmatched'
    if aggregation_name == 'timeline':
        # Insert unmatched rows into unmatched_events table if unmatched_event_settings is provided
        # Exclude synthetic EventId 901 (Phase Wait matched output - never unmatched by design)
        # Include 902 (Phase Wait pending) - this is how we track 43s that need Phase Wait matching
        # across chunks without interfering with PhaseCall matching of the same 43
        query += f""" CREATE TABLE unmatched_events AS
            SELECT StartTime AS TimeStamp, DeviceId, EventId, Parameter
            FROM timeline
            WHERE EndTime IS NULL AND EventId != 901; """
        # Delete unmatched rows from timeline table, including 902 (Phase Wait state tracking)
        # which is only used for incremental processing and should never appear in output
        query += f" DELETE FROM timeline WHERE EndTime IS NULL OR Duration < {kwargs['min_duration']} OR EventId = 902; "
        # Drop columns not needed in saved output
        query += " ALTER TABLE timeline DROP COLUMN Parameter; "
        query += " ALTER TABLE timeline DROP COLUMN EventId; "

    # For coordination_agg aggregation, insert synthetic state events into unmatched_events
    # Uses synthetic EventIds 931-934 to carry coordination state across incremental runs:
    # 931: Pattern, 932: CycleLength, 933: ActualCycleLength, 934: ActualOffset
    if aggregation_name == 'coordination_agg':
        query += """
        INSERT INTO unmatched_events
        SELECT 
            MAX(TimeStamp) AS TimeStamp,
            DeviceId,
            931 AS EventId,
            LAST(Pattern ORDER BY TimeStamp) AS Parameter
        FROM coordination_agg
        GROUP BY DeviceId
        HAVING LAST(Pattern ORDER BY TimeStamp) != 0;
        
        INSERT INTO unmatched_events
        SELECT 
            MAX(TimeStamp) AS TimeStamp,
            DeviceId,
            932 AS EventId,
            LAST(CycleLength ORDER BY TimeStamp) AS Parameter
        FROM coordination_agg
        GROUP BY DeviceId
        HAVING LAST(CycleLength ORDER BY TimeStamp) != 0;
        
        INSERT INTO unmatched_events
        SELECT 
            MAX(TimeStamp) AS TimeStamp,
            DeviceId,
            933 AS EventId,
            LAST(ActualCycleLength ORDER BY TimeStamp) AS Parameter
        FROM coordination_agg
        GROUP BY DeviceId
        HAVING LAST(ActualCycleLength ORDER BY TimeStamp) != 0;
        
        INSERT INTO unmatched_events
        SELECT 
            MAX(TimeStamp) AS TimeStamp,
            DeviceId,
            934 AS EventId,
            LAST(ActualOffset ORDER BY TimeStamp) AS Parameter
        FROM coordination_agg
        GROUP BY DeviceId
        HAVING LAST(ActualOffset ORDER BY TimeStamp) != 0;
        """

    try:
        if to_sql: # return sql as string
            return query
        # Otherwise, execute the query
        conn.query(query)
        return None
    except Exception as e:
        print('Error when executing query for: ', aggregation_name)
        #print(query)
        raise e