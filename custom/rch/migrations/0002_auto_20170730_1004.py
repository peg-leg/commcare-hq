# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-07-30 10:04
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rch', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='rchrecord',
            name='doc_type',
            field=models.CharField(choices=[(b'1', b'mother'), (b'2', b'child')], max_length=1),
        ),
    ]
