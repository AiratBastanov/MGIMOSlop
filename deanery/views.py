from django.db import connection, DatabaseError
from django.db.migrations.executor import MigrationExecutor
from django.shortcuts import render
from django.views.decorators.http import require_safe
from django.utils import timezone

@require_safe
def status_page(request):
    ready = False
    try:
        executor = MigrationExecutor(connection)
        ready = not executor.migration_plan(executor.loader.graph.leaf_nodes())
    except DatabaseError:
        connection.close()
    response = render(request, 'status.html', {'ready': ready, 'checked_at': timezone.now()},
                      status=200 if ready else 503)
    response['Cache-Control'] = 'no-store'
    return response
