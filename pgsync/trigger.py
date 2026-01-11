"""PGSync trigger template.

This module contains a template for creating a PostgreSQL trigger function that notifies updates asynchronously.
The trigger function constructs a notification as a JSON object and sends it to a channel using PG_NOTIFY.
The notification contains information about the updated table, the operation performed, the old and new rows, and the indices.
"""

from .constants import MATERIALIZED_VIEW, TRIGGER_FUNC

CREATE_TRIGGER_TEMPLATE = f"""
CREATE OR REPLACE FUNCTION {TRIGGER_FUNC}() RETURNS TRIGGER AS $$
DECLARE
  channel TEXT;
  old_row JSON;
  new_row JSON;
  notification JSON;
  xmin BIGINT;
  sme_id BIGINT;
  _indices TEXT [];
  _primary_keys TEXT [];
  _foreign_keys TEXT [];
  _columns TEXT [];
  _changed BOOLEAN;
  parent_table_name TEXT;
  partition_table_name TEXT;
  partition_info RECORD;

BEGIN
    -- database is also the channel name.
    channel := CURRENT_DATABASE();

    SELECT
        n.nspname as parent_schema,
        c.relname as parent_table,
        cn.nspname as partition_schema,
        cc.relname as partition_table,
        c.oid as parent_oid,
        cc.oid as partition_oid
    INTO partition_info
    FROM pg_catalog.pg_inherits i
    JOIN pg_catalog.pg_class cc ON cc.oid = i.inhrelid
    JOIN pg_catalog.pg_namespace cn ON cn.oid = cc.relnamespace
    JOIN pg_catalog.pg_class c ON c.oid = i.inhparent
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE cc.oid = (quote_ident(TG_TABLE_SCHEMA) || '.' || quote_ident(TG_TABLE_NAME))::regclass;

    -- Nếu tìm thấy parent, dùng parent_table
    IF partition_info.parent_table IS NOT NULL THEN
        parent_table_name := partition_info.parent_table;
    ELSE
        -- Không phải partition, dùng chính nó
        parent_table_name := TG_TABLE_NAME;
    END IF;

     -- Check if 'sme_id' exists in the OLD record
    IF to_jsonb(OLD) ? 'sme_id' THEN
        sme_id := OLD.sme_id;
    END IF;
    IF TG_OP = 'DELETE' THEN

        SELECT primary_keys, indices INTO _primary_keys, _indices
        FROM {MATERIALIZED_VIEW}
        WHERE table_name = parent_table_name;

        old_row = ROW_TO_JSON(OLD);
        old_row := (
            SELECT JSONB_OBJECT_AGG(key, value)
            FROM JSON_EACH(old_row)
            WHERE key = ANY(_primary_keys)
        )|| jsonb_build_object('sme_id', sme_id);
        xmin := OLD.xmin;
    ELSE
        IF TG_OP <> 'TRUNCATE' THEN

            SELECT primary_keys, foreign_keys, indices, columns
            INTO _primary_keys, _foreign_keys, _indices, _columns
            FROM {MATERIALIZED_VIEW}
            WHERE table_name = parent_table_name;

            -- normalize null to empty array
            _columns := COALESCE(_columns, ARRAY[]::TEXT[]);

            -- Only react if any _columns actually changed
            IF TG_OP = 'UPDATE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM JSONB_EACH(TO_JSONB(NEW.*)) n
                    JOIN JSONB_EACH(TO_JSONB(OLD.*)) o USING (key)
                    WHERE n.key = ANY(_columns)
                    AND n.value IS DISTINCT FROM o.value
                )
                INTO _changed;

                IF NOT _changed THEN
                    RETURN NEW;  -- skip notification; nothing relevant changed
                END IF;
            END IF;

            new_row = ROW_TO_JSON(NEW);
            new_row := (
                SELECT JSONB_OBJECT_AGG(key, value)
                FROM JSON_EACH(new_row)
                WHERE key = ANY(_primary_keys || _foreign_keys)
            )|| jsonb_build_object('sme_id', sme_id);
            IF TG_OP = 'UPDATE' THEN
                old_row = ROW_TO_JSON(OLD);
                old_row := (
                    SELECT JSONB_OBJECT_AGG(key, value)
                    FROM JSON_EACH(old_row)
                    WHERE key = ANY(_primary_keys || _foreign_keys)
                )|| jsonb_build_object('sme_id', sme_id);
            END IF;
            xmin := NEW.xmin;
        END IF;
    END IF;

    -- construct the notification as a JSON object.
    notification = JSON_BUILD_OBJECT(
        'xmin', xmin,
        'new', new_row,
        'old', old_row,
        'sme_id', sme_id,
        'indices', _indices,
        'tg_op', TG_OP,
        'table', parent_table_name,
        'schema', TG_TABLE_SCHEMA
    );

    -- Notify/Listen updates occur asynchronously,
    -- so this doesn't block the Postgres trigger procedure.
    PERFORM PG_NOTIFY(channel, notification::TEXT);

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
