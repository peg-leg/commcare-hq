from corehq.warehouse.const import DJANGO_MAX_BATCH_SIZE


def django_batch_records(cls, record_iter, field_mapping):
    records = []
    for index, raw_record in enumerate(record_iter):
        record = {}
        for source_key, destination_key in field_mapping:
            if isinstance(raw_record, dict):
                record[destination_key] = raw_record.get(source_key)
            else:
                record[destination_key] = getattr(raw_record, source_key, None)

        records.append(cls(**record))

    cls.objects.bulk_create(records, batch_size=DJANGO_MAX_BATCH_SIZE)
