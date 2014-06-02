from django.http import HttpResponse

from wagtail.wagtailcore import hooks

def editor_css():
    return """<link rel="stylesheet" href="/path/to/my/custom.css">"""
hooks.register('insert_editor_css', editor_css)


def editor_js():
    return """<script src="/path/to/my/custom.js"></script>"""
hooks.register('insert_editor_js', editor_js)


def block_googlebot(page, request):
    if request.META.get('HTTP_USER_AGENT') == 'GoogleBot':
        return HttpResponse("<h1>bad googlebot no cookie</h1>")
hooks.register('before_serve_page', block_googlebot)
