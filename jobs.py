import json

from django.apps import apps
from django.core.exceptions import (
    FieldError,
    ObjectDoesNotExist,
    ValidationError,
    MultipleObjectsReturned,
)
from django.db import transaction
from django.db.utils import IntegrityError
from nautobot.apps.jobs import Job, TextVar


name = "Intended State"


def replace_ref(ref):
    """Recursively replace references."""
    if isinstance(ref, dict):
        for key, value in ref.items():
            ref[key] = replace_ref(value)
        return ref

    if isinstance(ref, (list, set, tuple)):
        return [replace_ref(r) for r in ref]

    if not isinstance(ref, (str, bytes)):
        return ref

    if not ref.startswith("#ref"):
        return ref

    ref_split = ref.split(":")
    # pop off #ref
    ref_split.pop(0)
    # Grab the app.model
    app_name = ref_split.pop(0)
    object_class = apps.get_model(app_name)
    # Get every other item in the list for fields
    fields = [f for f in ref_split[::2]]
    # and for values start at index 1
    values = [v for v in ref_split[1::2]]
    obj_lookup = {fields[i]: values[i] for i in range(len(fields))}
    return object_class.objects.get(**obj_lookup)


def obj_set(obj, set_dict):
    for field_name, set_list in set_dict.items():
        if not isinstance(set_list, (list, set, tuple)):
            set_list = [set_list]
        field = getattr(obj, field_name)
        field.set(set_list, clear=True)
    obj.validated_save()


def obj_add(obj, add_dict):
    for field_name, add_list in add_dict.items():
        if not isinstance(add_list, (list, set, tuple)):
            add_list = [add_list]
        field = getattr(obj, field_name)
        field.add(*add_list)
    obj.validated_save()


class IntendedState(Job):
    json_payload = TextVar()

    class Meta:
        name = "Intended State Job"
        description = "Create or update objects in Nautobot by passing in an intended state JSON payload."

    def run(self, json_payload):
        try:
            with transaction.atomic():
                self._run_intended_state(json_payload)
        except Exception as error:
            self.logger.error("Failed to create objects: %s", error)
            raise error

    def _run_intended_state(self, json_payload):
        intended_state = json.loads(json_payload)
        for object_name, objects in intended_state.items():
            object_class = apps.get_model(object_name)
            for object_data in objects:
                if "#set" in object_data:
                    set_dict = replace_ref(object_data.pop("#set"))
                elif "#set" in object_data.get("defaults", {}):
                    set_dict = replace_ref(object_data["defaults"].pop("#set"))
                else:
                    set_dict = None
                if "#add" in object_data:
                    add_dict = replace_ref(object_data.pop("#add"))
                elif "#add" in object_data.get("defaults", {}):
                    add_dict = replace_ref(object_data["defaults"].pop("#add"))
                else:
                    add_dict = None
                try:
                    for key, value in object_data.items():
                        object_data[key] = replace_ref(value)
                except (
                    AttributeError,
                    LookupError,
                    ObjectDoesNotExist,
                    ValidationError,
                    MultipleObjectsReturned,
                ) as e:
                    self.logger.warning(
                        "Error replacing reference on %s. Error: %s.", object_name, e
                    )
                    continue
                try:
                    obj, created = object_class.objects.update_or_create(**object_data)
                    if set_dict:
                        obj_set(obj, set_dict)
                    if add_dict:
                        obj_add(obj, add_dict)
                except (
                    ValueError,
                    FieldError,
                    ObjectDoesNotExist,
                    ValidationError,
                    MultipleObjectsReturned,
                    IntegrityError,
                ) as e:
                    self.logger.warning("Unable to create object. Error: %s.", e)
                    continue
                self.logger.info(
                    "Object %s has been %s.",
                    obj,
                    "created" if created else "updated",
                    extra={"object": obj},
                )
