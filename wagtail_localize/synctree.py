"""
Tree Synchronisation

This module contains all the logic for for synchronising language trees.

This provides the following functionality:

 - Creating and updating placeholder pages for content that hasn't been translated yet
 - Moving pages so that they always match their position in their source locale
"""

from collections import defaultdict
import functools

from django.conf import settings
from django.conf.locale import LANG_INFO
from django.core.signals import setting_changed
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.functional import cached_property
from wagtail.core.models import Page, Locale
from wagtail.core.utils import get_content_languages, get_supported_content_language_variant


def pk(obj):
    if isinstance(obj, models.Model):
        return obj.pk
    else:
        return obj


@functools.lru_cache(maxsize=1000)
def get_fallback_content_languages(lang_code):
    """
    Returns a list of language codes that can be used as a fallback to the given language
    """
    possible_lang_codes = []

    # Check if this is a special-case
    try:
        possible_lang_codes.extend(LANG_INFO[lang_code]["fallback"])
    except (KeyError, IndexError):
        pass

    # Convert region specific language codes into generic (eg fr-ca => fr)
    generic_lang_code = lang_code.split("-")[0]
    supported_lang_codes = get_content_languages()

    possible_lang_codes.append(generic_lang_code)

    # Try other regions with the same language
    for supported_code in supported_lang_codes:
        if supported_code.startswith(generic_lang_code + "-"):
            possible_lang_codes.append(supported_code)

    # Finally try the default language
    possible_lang_codes.append(get_supported_content_language_variant(settings.LANGUAGE_CODE))

    # Remove lang_code and any duplicates
    seen = {lang_code}
    deduplicated_lang_codes = []
    for possible_lang_code in possible_lang_codes:
        if possible_lang_code not in seen:
            deduplicated_lang_codes.append(possible_lang_code)
            seen.add(possible_lang_code)

    return deduplicated_lang_codes


@receiver(setting_changed)
def reset_cache(**kwargs):
    """
    Clear cache when global WAGTAIL_CONTENT_LANGUAGES/LANGUAGES/LANGUAGE_CODE settings are changed
    """
    if kwargs["setting"] in ("WAGTAIL_CONTENT_LANGUAGES", "LANGUAGES", "LANGUAGE_CODE"):
        get_fallback_content_languages.cache_clear()


def get_fallback_locales(locale):
    """
    Returns a queryset of locales that this locale can fall back to if there
    isn't a translation.
    For example, es-MX can fall back to es, es-ES, etc.
    """
    fallback_languages = get_fallback_content_languages(locale.language_code)
    fallback_locale_map = {
        locale.language_code: locale
        for locale in Locale.objects.filter(
            language_code__in=fallback_languages
        )
    }
    return [fallback_locale_map[lang_code] for lang_code in fallback_languages]


def get_best_fallback_locale(locale, fallback_locales):
    """
    Chooses the best fallback locale from the available list of locales.
    The locales must be an iterable of locale instances or ids. This always
    returns a Locale instance.
    """
    locale_ids = set(pk(locale) for locale in fallback_locales)
    for locale in get_fallback_locales(locale):
        if locale.id in locale_ids:
            return locale


class PageIndex:
    """
    An in-memory index of pages to remove the need to query the database.

    Each entry in the index is a unique page by transaction key, so a page
    that has been translated into different languages appears only once.
    """

    # Note: This has been designed to be as memory-efficient as possible, but it
    # hasn't been tested on a very large site yet.

    class Entry:
        """
        Represents a page in the index.
        """

        __slots__ = [
            "content_type",
            "translation_key",
            "source_locale",
            "parent_translation_key",
            "locales",
            "aliased_locales",
        ]

        def __init__(
            self,
            content_type,
            translation_key,
            source_locale,
            parent_translation_key,
            locales,
            aliased_locales,
        ):
            self.content_type = content_type
            self.translation_key = translation_key
            self.source_locale = source_locale
            self.parent_translation_key = parent_translation_key
            self.locales = locales
            self.aliased_locales = aliased_locales

        REQUIRED_PAGE_FIELDS = [
            "content_type",
            "translation_key",
            "locale",
            "path",
            "depth",
            "last_published_at",
            "latest_revision_created_at",
            "live",
        ]

        @classmethod
        def from_page_instance(cls, page):
            """
            Initialises an Entry from the given page instance.
            """
            # Get parent, but only if the parent is not the root page. We consider the
            # homepage of each langauge tree to be the roots
            if page.depth > 2:
                parent_page = page.get_parent().specific
            else:
                parent_page = None

            return cls(
                page.content_type,
                page.translation_key,
                page.locale,
                parent_page.translation_key if parent_page else None,
                list(
                    Page.objects.filter(
                        translation_key=page.translation_key,
                        alias_of__isnull=True,
                    ).values_list("locale", flat=True)
                ),
                list(
                    Page.objects.filter(
                        translation_key=page.translation_key,
                        alias_of__isnull=False,
                    ).values_list("locale", flat=True)
                ),
            )

    def __init__(self, pages):
        self.pages = pages

    @cached_property
    def by_translation_key(self):
        return {page.translation_key: page for page in self.pages}

    @cached_property
    def by_parent_translation_key(self):
        by_parent_translation_key = defaultdict(list)
        for page in self.pages:
            by_parent_translation_key[page.parent_translation_key].append(page)

        return dict(by_parent_translation_key.items())

    def sort_by_tree_position(self):
        """
        Returns a new index with the pages sorted in depth-first-search order
        using their parent in their respective source locale.
        """
        remaining_pages = set(page.translation_key for page in self.pages)

        new_pages = []

        def _walk(translation_key):
            for page in self.by_parent_translation_key.get(translation_key, []):
                if page.translation_key not in remaining_pages:
                    continue

                remaining_pages.remove(page.translation_key)
                new_pages.append(page)
                _walk(page.translation_key)

        _walk(None)

        if remaining_pages:
            print("Warning: {} orphaned pages!".format(len(remaining_pages)))

        return PageIndex(new_pages)

    def not_translated_into(self, locale):
        """
        Returns an index of pages that are not translated into the specified locale.
        This includes pages that have and don't have a placeholder
        """
        pages = [page for page in self.pages if locale.id not in page.locales]

        return PageIndex(pages)

    def __iter__(self):
        return iter(self.pages)

    @classmethod
    def from_database(cls):
        """
        Populates the index from the database.
        """
        pages = []

        for page in Page.objects.filter(alias_of__isnull=True, depth__gt=1).only(
            *PageIndex.Entry.REQUIRED_PAGE_FIELDS
        ):
            pages.append(PageIndex.Entry.from_page_instance(page))

        return PageIndex(pages)


def synchronize_tree(page_index, locale):
    """
    Synchronises a locale tree with the other locales.

    This creates any placeholders that don't exist yet, updates placeholders where their
    source has been changed and moves pages to match the structure of other trees
    """
    # Find pages that are not translated for this locale
    # This includes locales that have a placeholder, it only excludes locales that have an actual translation
    pages_not_in_locale = page_index.not_translated_into(locale)

    for page in pages_not_in_locale:
        # TODO: Is it wise to assume all pages should have aliases in other languages,
        # even if that page doesn't have a good fallback locale?
        source_locale = get_best_fallback_locale(locale, page.locales) or page.locales[0]

        # Fetch source from database
        model = page.content_type.model_class()
        source_page = model.objects.get(
            translation_key=page.translation_key, locale=source_locale
        )

        if locale.id not in page.aliased_locales:
            print(f"Copying '{source_page}' into '{locale}'")
            source_page.copy_for_translation(
                locale, copy_parents=True, alias=True
            )


@receiver(post_save)
def on_page_saved(sender, instance, **kwargs):
    """
    Called whenever a page is saved, whether this is a:
     - Page creation
     - Page edit
     - Creation/edit of a translation
     - Creation/edit of a placeholder (ignored)
    """
    if not getattr(settings, "WAGTAILLOCALIZE_ENABLE_PLACEHOLDERS", False):
        return

    # Is this a creation or an edit?
    is_creation = kwargs["created"]

    # Is this a source, translation or an alias?
    if instance.is_source_translation:
        page_type = "source"
    elif instance.alias_of_id is not None:
        page_type = "alias"
    else:
        page_type = "translation"

    # We're not interested in reacting to when aliases are updated
    if page_type == "alias":
        return

    if is_creation:
        # New page is created
        # If this is the source, we need to copy for all locales
        # If this is a translation, we need to check existing aliases and change their
        # source locale if this translation is better than the source they already have

        # Create aliases all in locales that don't have one yet
        missing_locales = Locale.objects.exclude(
            id__in=instance.__class__.objects.filter(
                translation_key=instance.translation_key,
            ).values_list("locale_id", flat=True)
        )

        for locale in missing_locales:
            instance.copy_for_translation(
                locale, copy_parents=True, alias=True, keep_live=True
            )

        # Check existing aliases to see if this translation would be a better source for them.
        # For example, say we already have a page in en with aliases in es/es-MX. When the page
        # is translated into es, we want to change the placeholder_locale of the es-MX to es.
        if page_type == "translation":
            # A queryset of locales that have translations
            translated_locales = Locale.objects.exclude(
                id__in=instance.__class__.objects.filter(
                    translation_key=instance.translation_key,
                    alias_of__isnull=True,
                ).values_list("locale_id", flat=True)
            )

            # A queryset of locales that are currently aliases
            aliased_locales = Locale.objects.exclude(
                id__in=instance.__class__.objects.filter(
                    translation_key=instance.translation_key,
                    alias_of__isnull=False,
                ).values_list("locale_id", flat=True)
            )

            # Find locales where this new translation provides a better fallback
            for locale in aliased_locales:
                best_fallback_locale = get_best_fallback_locale(locale, translated_locales)

                if best_fallback_locale == instance.locale:
                    placeholder = instance.get_translation(locale)
                    placeholder.alias_of = instance

                    # TODO: Sync alias here
