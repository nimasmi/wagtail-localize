from django.core.management.base import BaseCommand
from wagtail.core.models import Locale

from wagtail_localize.synctree import PageIndex, synchronize_tree


class Command(BaseCommand):
    help = "Synchronises the structure of all locale page trees so they contain the same pages. Creates alias pages where necessary."

    def handle(self, **options):
        # Get an index of all pages
        index = PageIndex.from_database().sort_by_tree_position()

        for locale in Locale.objects.all():
            synchronize_tree(index, locale)
