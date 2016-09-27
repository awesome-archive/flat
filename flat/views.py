from __future__ import print_function, unicode_literals, division, absolute_import
import os
import json
import datetime
import sys
from collections import defaultdict

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect, HttpRequest
import django.contrib.auth
from django.conf import settings

from pynlpl.formats import fql

import flat.comm
import flat.users
from flat.converters import get_converters, inputformatchangefunction
from flat.modes.metadata.models import MetadataIndex
import lxml.html
if sys.version < '3':
    from urllib2 import URLError, HTTPError #pylint: disable=import-error
else:
    from urllib.error import URLError, HTTPError

def getcontext(request,namespace,docid, doc, mode):
    return {
            'configuration': settings.CONFIGURATIONS[request.session['configuration']],
            'configuration_json': json.dumps(settings.CONFIGURATIONS[request.session['configuration']]),
            'namespace': namespace,
            'testnum': request.GET.get('testNumber',0),
            'docid': docid,
            'mode': mode,
            'modes': settings.CONFIGURATIONS[request.session['configuration']]['modes'] ,
            'modes_json': json.dumps([x[0] for x in settings.CONFIGURATIONS[request.session['configuration']]['modes'] ]),
            'perspectives_json': json.dumps(settings.CONFIGURATIONS[request.session['configuration']]['perspectives']),
            'docdeclarations': json.dumps(doc['declarations']) if 'declarations' in doc else "{}",
            'setdefinitions': json.dumps(doc['setdefinitions']) if 'setdefinitions' in doc else "{}",
            'metadata': json.dumps(doc['metadata']) if 'metadata' in doc else "{}",
            'toc': json.dumps(doc['toc']) if 'toc' in doc else "[]",
            'slices': json.dumps(doc['slices']) if 'slices' in doc else "{}",
            'rtl': True if 'rtl' in doc and doc['rtl'] else False,
            'loggedin': request.user.is_authenticated(),
            'isadmin': request.user.is_staff,
            'version': settings.VERSION,
            'username': request.user.username,
            'waitmessage': "Loading document on server and initialising web-environment...",
    }

def validatenamespace(namespace):
    return namespace.replace('..','').replace('"','').replace(' ','_').replace(';','').replace('&','').strip('/')

def getdocumentselector(query):
    if query.startswith("USE "):
        end = query[4:].index(' ') + 4
        if end >= 0:
            try:
                namespace,docid = query[4:end].rsplit("/",1)
            except:
                raise fql.SyntaxError("USE statement takes namespace/docid pair")
            return (validatenamespace(namespace),docid), query[end+1:]
        else:
            try:
                namespace,docid = query[4:end].rsplit("/",1)
            except:
                raise fql.SyntaxError("USE statement takes namespace/docid pair")
            return (validatenamespace(namespace),docid), ""
    return None, query

def getbody(html):
    doc = lxml.html.fromstring(html)
    return doc.xpath("//body/p")[0].text_content()

def docserveerror(e, d=None):
    if d is None: d={}
    if isinstance(e, HTTPError):
        body = getbody(e.read())
        d['fatalerror'] =  "<strong>Fatal Error:</strong> The document server returned an error<pre style=\"font-weight: bold\">" + str(e) + "</pre><pre>" + body +"</pre>"
        d['fatalerror_text'] = body
    elif isinstance(e, URLError):
        d['fatalerror'] =  "<strong>Fatal Error:</strong> Could not connect to document server!"
        d['fatalerror_text'] =  "Could not connect to document server!"
    elif isinstance(e, str) :
        if sys.version < '3':
            d['fatalerror'] =   e.decode('utf-8') #pylint: disable=undefined-variable
            d['fatalerror_text'] = e.decode('utf-8') #pylint: disable=undefined-variable
        else:
            d['fatalerror'] =  e
            d['fatalerror_text'] = e
    elif sys.version < '3' and isinstance(e, unicode): #pylint: disable=undefined-variable
        d['fatalerror'] =  e
        d['fatalerror_text'] = e
    elif isinstance(e, Exception):
        # we don't handle other exceptions, raise!
        raise e
    return d


def initdoc(request, namespace, docid, mode, template, context=None):
    """Initialise a document (not invoked directly)"""
    perspective = request.GET.get('perspective','document')
    if context is None: context = {}
    flatargs = {
        'setdefinitions': True,
        'declarations': True,
        'metadata': True,
        'toc': True,
        'slices': request.GET.get('slices',settings.CONFIGURATIONS[request.session['configuration']].get('slices','p:25,s:100')), #overriden either by configuration or by user
        'customslicesize': 0, #disabled for initial probe
        'textclasses': True,
    }
    try:
        doc = flat.comm.query(request, "USE " + namespace + "/" + docid + " PROBE", **flatargs) #retrieves only the meta information, not document content
        context.update(getcontext(request,namespace,docid, doc, mode))
    except Exception as e:
        context.update(docserveerror(e))
    dorequiredeclaration = 'requiredeclaration' in settings.CONFIGURATIONS[request.session['configuration']] and settings.CONFIGURATIONS[request.session['configuration']]['requiredeclaration']
    if dorequiredeclaration:
        declarations = json.loads(context['declarations'])
        for annotationtype, annotationset in settings.CONFIGURATIONS[request.session['configuration']]['requiredeclaration']:
            if annotationtype not in declarations or (annotationset and annotationset not in declarations[annotationtype]):
                if annotationset:
                    return fatalerror(request, "Refusing to load document, missing expected declaration for annotation type " + annotationtype + "/" + annotationset)
                else:
                    return fatalerror(request, "Refusing to load document, missing expected declaration for annotation type " + annotationtype)

    dometadataindex = 'metadataindex' in settings.CONFIGURATIONS[request.session['configuration']] and settings.CONFIGURATIONS[request.session['configuration']]['metadataindex']
    if dometadataindex:
        metadata = json.loads(context['metadata'])
        for metakey in settings.CONFIGURATIONS[request.session['configuration']]['metadataindex']:
            if metakey in metadata:
                MetadataIndex.objects.update_or_create(namespace=namespace,docid=docid, key=metakey,value=metadata[metakey])
    response = render(request, template, context)
    if 'fatalerror' in context:
        response.status_code = 500
    return response

@login_required
def query(request,namespace, docid):
    if request.method != 'POST':
        return HttpResponseForbidden("POST method required for " + namespace + "/" + docid + "/query")

    flatargs = {
        'customslicesize': request.POST.get('customslicesize',settings.CONFIGURATIONS[request.session['configuration']].get('customslicesize','50')), #for pagination of search results
    }

    if flat.users.models.hasreadpermission(request.user.username, namespace, request):

        #stupid compatibility stuff
        if sys.version < '3':
            if hasattr(request, 'body'):
                data = json.loads(unicode(request.body,'utf-8')) #pylint: disable=undefined-variable
            else: #older django
                data = json.loads(unicode(request.raw_post_data,'utf-8')) #pylint: disable=undefined-variable
        else:
            if hasattr(request, 'body'):
                data = json.loads(str(request.body,'utf-8'))
            else: #older django
                data = json.loads(str(request.raw_post_data,'utf-8'))

        if not data['queries']:
            return HttpResponseForbidden("No queries to run")

        for query in data['queries']:
            #get document selector and check it doesn't violate the namespace
            docselector, query = getdocumentselector(query)
            if not docselector:
                return HttpResponseForbidden("Query does not start with a valid document selector (USE keyword)!")
            elif docselector[0] != namespace:
                return HttpResponseForbidden("Query would affect a different namespace than your current one, forbidden!")

            if query != "GET" and query[:4] != "CQL " and query[:4] != "META":
                #parse query on this end to catch syntax errors prior to sending, should be fast enough anyway
                try:
                    query = fql.Query(query)
                except fql.SyntaxError as e:
                    return HttpResponseForbidden("FQL Syntax Error: " + str(e))
                needwritepermission = query.declarations or query.action and query.action.action != "SELECT"
            else:
                needwritepermission = False

        if needwritepermission and not flat.users.models.haswritepermission(request.user.username, namespace, request):
            return HttpResponseForbidden("Permission denied, no write access")

        query = "\n".join(data['queries']) #throw all queries on a big pile to transmit
        try:
            d = flat.comm.query(request, query,**flatargs)
        except Exception as e:
            if sys.version < '3':
                errmsg = docserveerror(e)['fatalerror_text']
                return HttpResponseForbidden("FoLiA Document Server error: ".encode('utf-8') + errmsg.encode('utf-8'))
            else:
                return HttpResponseForbidden("FoLiA Document Server error: " + docserveerror(e)['fatalerror_text'])
        return HttpResponse(json.dumps(d).encode('utf-8'), content_type='application/json')
    else:
        return HttpResponseForbidden("Permission denied, no read access")

def login(request):
    if 'username' in request.POST and 'password' in request.POST:
        username = request.POST['username']
        password = request.POST['password']
        request.session['configuration'] = request.POST['configuration']
        user = django.contrib.auth.authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                django.contrib.auth.login(request, user)
                # Redirect to a success page.
                if 'next' in request.POST:
                    return redirect("/" + request.POST['next'])
                elif 'next' in request.GET:
                    return redirect("/" + request.GET['next'])
                else:
                    return redirect("/")
            else:
                # Return a 'disabled account' error message
                return render(request, 'login.html', {'error': "This account is disabled","defaultconfiguration":settings.DEFAULTCONFIGURATION, "configurations":settings.CONFIGURATIONS , 'version': settings.VERSION} )
        else:
            # Return an 'invalid login' error message.
            return render(request, 'login.html', {'error': "Invalid username or password","defaultconfiguration":settings.DEFAULTCONFIGURATION, "configurations":settings.CONFIGURATIONS, 'version': settings.VERSION} )
    else:
        return render(request, 'login.html',{"defaultconfiguration":settings.DEFAULTCONFIGURATION, "configurations":settings.CONFIGURATIONS, "version": settings.VERSION})


def logout(request):
    if 'configuration' in request.session:
        del request.session['configuration']
    django.contrib.auth.logout(request)
    return redirect("/login")


def register(request):
    if request.method == 'POST':
        form = django.contrib.auth.forms.UserCreationForm(request.POST)
        if form.is_valid():
            new_user = form.save()
            return HttpResponseRedirect("/login/")
    else:
        form = django.contrib.auth.forms.UserCreationForm()
    return render(request, "register.html", {
        'form': form,
        'version': settings.VERSION,
    })

def fatalerror(request, e,code=404):
    assert isinstance(request, HttpRequest)
    if isinstance(e, Exception):
        response = render(request,'base.html', docserveerror(e))
    else:
        response = render(request,'base.html', {'fatalerror': str(e)})
    response.status_code = code
    return response


@login_required
def index(request, namespace=""):
    try:
        namespaces = flat.comm.get(request, '/namespaces/')
    except Exception as e:
        return fatalerror(request,e)

    if not namespace:
        #check if user namespace is preset, if not, make it
        if not request.user.username in namespaces['namespaces']:
            try:
                flat.comm.get(request, "createnamespace/" + request.user.username, False)
            except Exception as e:
                return fatalerror(request,e)

        if 'creategroupnamespaces' in settings.CONFIGURATIONS[request.session['configuration']] and settings.CONFIGURATIONS[request.session['configuration']]['creategroupnamespaces'] and request.user.has_perm('groupwrite'):
            for group in request.user.groups.all():
                try:
                    flat.comm.get(request, "createnamespace/" + group.name, False)
                except Exception as e:
                    return fatalerror(request,e)

        if request.user.has_perm('groupwrite') and (request.user.has_perm('allowcopy') or request.user.has_perm('allowmove')):
            #create namespaces for all the users in our groups, since we may want to copy there
            for group in request.user.groups.all():
                for user in django.contrib.auth.models.User.objects.filter(groups__name=group.name):
                    try:
                        flat.comm.get(request, "createnamespace/" + user.username, False)
                    except Exception as e:
                        return fatalerror(request,e)

    readpermission = flat.users.models.hasreadpermission(request.user.username, namespace, request)
    dirs = []
    recursivedirs =  []
    subdirs =[]
    for ns in sorted(namespaces['namespaces']):
        if readpermission or flat.users.models.hasreadpermission(request.user.username, os.path.join(namespace, ns), request):
            recursivedirs.append(ns)
            if '/' not in ns:
                dirs.append(ns)
                if not namespace:
                    subdirs.append(ns)
            if ns.startswith(namespace + '/'):
                subdirs.append(ns[len(namespace)+1:])
    dirs.sort()
    recursivedirs.sort()

    dometadataindex = 'metadataindex' in settings.CONFIGURATIONS[request.session['configuration']] and settings.CONFIGURATIONS[request.session['configuration']]['metadataindex']
    if dometadataindex:
        metadataindex = defaultdict(list)
        for m in MetadataIndex.objects.filter(namespace=namespace):
            metadataindex[m.docid].append( (m.key, m.value) )

    docs = []
    if namespace and readpermission:
        try:
            r = flat.comm.get(request, '/documents/' + namespace)
        except Exception as e:
            return fatalerror(request,e)
        for d in sorted(r['documents']):
            docid =  os.path.basename(d.replace('.folia.xml',''))
            if dometadataindex and docid in metadataindex:
                metaitems = metadataindex[docid]
            else:
                metaitems = []
            docs.append( (docid, round(r['filesize'][d] / 1024 / 1024,2) , datetime.datetime.fromtimestamp(r['timestamp'][d]).strftime("%Y-%m-%d %H:%M"), metaitems ) )

    if not 'configuration' in request.session:
        return logout(request)

    docs.sort()

    if namespace:
        parentdir = '/'.join(namespace.split('/')[:-1])
    else:
        parentdir = ""

    return render(request, 'index.html', {'namespace': namespace,'parentdir': parentdir, 'dirs': dirs, 'recursivedirs': recursivedirs, 'subdirs': subdirs, 'docs': docs, 'defaultmode': settings.DEFAULTMODE,'loggedin': request.user.is_authenticated(), 'isadmin': request.user.is_staff, 'username': request.user.username, 'configuration': settings.CONFIGURATIONS[request.session['configuration']], 'converters': get_converters(request), 'inputformatchangefunction': inputformatchangefunction(request), 'allowcopy': request.user.has_perm('allowcopy'), 'allowdelete': request.user.has_perm('allowdelete'),'version': settings.VERSION})

@login_required
def download(request, namespace, docid):
    data = flat.comm.query(request, "USE " + namespace + "/" + docid + " GET",False)
    return HttpResponse(data, content_type='text/xml')

@login_required
def filemanagement(request):
    if request.method == 'POST':
        if 'filemanmode' in request.POST:
            if request.POST['filemanmode'] == 'delete':
                targetnamespace = None
            elif request.POST['filemanmode'] == 'copy' and 'copytarget' in request.POST and request.POST['copytarget']:
                targetnamespace = request.POST['copytarget'].replace('..','.').replace(' ','').replace('&','')
            elif request.POST['filemanmode'] == 'move' and 'movetarget' in request.POST and request.POST['movetarget']:
                targetnamespace = request.POST['movetarget'].replace('..','.').replace(' ','').replace('&','')
            else:
                return fatalerror(request, "Invalid operation",404)
 
            docs = []
            for key, value in request.POST.items():
                if key.startswith('docselect'):
                    docs.append(value)
            if not docs:
                return fatalerror(request, "No documents selected",404)

            if targetnamespace:
                if not flat.users.models.haswritepermission(request.user.username, targetnamespace, request):
                    return fatalerror(request, "Write permission denied for " + targetnamespace,403)

            namespace = None
            for doc in docs:
                namespace, docid = doc.rsplit('/', 1)
                if targetnamespace:
                    data = {'target': targetnamespace + '/' + docid}
                else:
                    data = {}

                if not flat.users.models.hasreadpermission(request.user.username, namespace, request):
                    return fatalerror(request, "Read permission denied for " + docid,403)
                if request.POST['filemanmode'] in ('delete','move') and not flat.users.models.haswritepermission(request.user.username, namespace, request):
                    return fatalerror(request, "Write permission denied for " + docid,403)

                try:
                    flat.comm.filemanagement(request,request.POST['filemanmode'], namespace, docid, **data)
                except Exception as e:
                    return fatalerror(request,e)

            if targetnamespace:
                return HttpResponseRedirect("/index/" + targetnamespace )
            elif namespace:
                return HttpResponseRedirect("/index/" + namespace )
            else:
                return HttpResponseRedirect("/")

        else:
            return fatalerror(request, "No mode specified",403)
    else:
        return fatalerror(request, "Method denied",403)

@login_required
def upload(request):
    if request.method == 'POST':
        namespace = request.POST['namespace'].replace('..','.').replace(' ','').replace('&','')
        if flat.users.models.haswritepermission(request.user.username, namespace, request) and 'file' in request.FILES:
            if request.POST['inputformat'] != 'folia':
                #we need to convert the input
                converter = None
                for cv in get_converters(request):
                    if request.POST['inputformat'] == cv.id:
                        converter = cv
                if not converter:
                    return fatalerror(request, "Converter not found or specified",404)

                #try:
                parameters = converter.parse_parameters(request, 'parameters')
                #except:
                #return fatalerror(request, "Invalid syntax for conversion parameters",403)

                if 'TMPDIR' in os.environ:
                    tmpdir = os.environ['TMPDIR']
                else:
                    tmpdir = '/tmp'

                tmpinfile = os.path.join(tmpdir, request.FILES['file'].name)
                with open(tmpinfile,'wb') as f_tmp:
                    for chunk in request.FILES['file'].chunks():
                        f_tmp.write(chunk)

                tmpoutfile = converter.get_output_name(os.path.join(tmpdir, request.FILES['file'].name))
                success, msg = converter.convert(tmpinfile, tmpoutfile, **parameters)
                if not success:
                    return fatalerror(request, "Input conversion failed: " + msg,403)

                #removing temporary input file
                os.unlink(tmpinfile)

                #reading temporary output file
                f_folia =  open(tmpoutfile, 'rb')
            else:
                f_folia = request.FILES['file']

            #if sys.version < '3':
            #    data = unicode(request.FILES['file'].read(),'utf-8') #pylint: disable=undefined-variable
            #else:
            #    data = str(request.FILES['file'].read(),'utf-8')
            failed = False
            try:
                response = flat.comm.postxml(request,"upload/" + namespace , f_folia)
            except Exception as e:
                failed = True
                response = fatalerror(request,e)

            if request.POST['inputformat'] != 'folia':
                f_folia.close()
                #removing temporary output file after conversion
                os.unlink(tmpoutfile)

            if failed:
                return response
            elif 'error' in response and response['error']:
                return fatalerror(request, response['error'],403)
            else:
                docid = response['docid']
                return HttpResponseRedirect("/" + settings.DEFAULTMODE + "/" + namespace + "/" + docid  )
        else:
            return fatalerror(request, "Write permission denied",403)
    else:
        return fatalerror(request, "Method denied",403)


@login_required
def addnamespace(request):
    if request.method == 'POST':
        namespace = request.POST['namespace'].replace('/','').replace('..','.').replace(' ','').replace('&','')
        newdirectory = request.POST['newdirectory'].replace('/','').replace('..','.').replace(' ','').replace('&','')
        if flat.users.models.haswritepermission(request.user.username, namespace, request):
            try:
                response = flat.comm.get(request,"createnamespace/" + namespace + "/" + newdirectory)
            except Exception as e:
                return fatalerror(request,e)
            if 'error' in response and response['error']:
                return fatalerror(request, response['error'],403)
            elif namespace:
                return HttpResponseRedirect("/index/" + namespace + '/' + newdirectory  )
            else:
                return HttpResponseRedirect("/index/" + newdirectory  )
        else:
            return fatalerror(request, "Permission denied",403)
    else:
        return fatalerror(request, "Permission denied",403)
