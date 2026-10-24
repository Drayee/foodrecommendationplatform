from django.apps import AppConfig


class PlacesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "places"
    verbose_name = "附近美食（高德 POI）"
