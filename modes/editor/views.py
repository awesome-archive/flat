from __future__ import print_function, unicode_literals, division, absolute_import
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
import flat.comm as comm
import flat.settings as settings
import json

@login_required
def view(request, docid):
    doc = comm.get(request, '/getdoc/%NS%/' + docid + '/')
    d = {
            'docid': docid,
            'mode': 'editor',
            'modes': settings.EDITOR_MODES,
            'modes_json': json.dumps([x[0] for x in settings.EDITOR_MODES]),
            'dochtml': doc['html'],
            'docannotations': json.dumps(doc['annotations']),
            'docdeclarations': json.dumps(doc['declarations']),
            'loggedin': request.user.is_authenticated(),
            'username': request.user.username
    }
    #TODO later: add setdefinitions
    return render(request, 'editor.html', d)


@login_required
def annotate(request, docid):
    e = comm.postjson(request, '/annotate/%NS%/' + docid + '/', request.body)
    d = {
            'elementid': e['elementid'],
            'html': e['html'],
            'annotations': json.dumps(e['annotations']),
    }
    return HttpResponse(json.dumps(d), mimetype='application/json')



