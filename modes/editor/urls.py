from django.conf.urls import patterns, include, url

urlpatterns = patterns('',
    # Examples:
    url(r'^(?P<docid>[\w\-\.]+)/?$', 'flat.modes.editor.views.view', name='view'),
    url(r'^(?P<docid>[\w\-\.]+)/annotate/?$', 'flat.modes.editor.views.annotate', name='annotate'),

)