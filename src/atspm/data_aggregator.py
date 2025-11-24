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
        query += f""" CREATE TABLE unmatched_events AS
            SELECT StartTime AS TimeStamp, DeviceId, EventId, Parameter
            FROM timeline WHERE EndTime IS NULL; """
        # And drop unmatched rows from timeline table
        query += f" DELETE FROM timeline WHERE EndTime IS NULL OR Duration < {kwargs['min_duration']}; "
        # Drop EventId and Parameter from timeline table
        query += " ALTER TABLE timeline DROP COLUMN EventId; "
        query += " ALTER TABLE timeline DROP COLUMN Parameter; "

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
