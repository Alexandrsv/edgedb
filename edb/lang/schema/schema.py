#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import functools
import itertools
import typing

import immutables as immu

from . import casts as s_casts
from . import error as s_err
from . import functions as s_func
from . import modules as s_modules
from . import name as sn
from . import objects as so
from . import operators as s_oper
from . import types as s_types


STD_LIB = ['std', 'schema', 'math']
STD_MODULES = {'std', 'schema', 'stdgraphql', 'math'}


_void = object()


class Schema:

    def __init__(self):
        self._modules = immu.Map()
        self._id_to_data = immu.Map()
        self._id_to_type = immu.Map()
        self._shortname_to_id = immu.Map()
        self._name_to_id = immu.Map()
        self._refs_to = immu.Map()
        self._generation = 0

    def _replace(self, *, id_to_data=None, id_to_type=None,
                 name_to_id=None, shortname_to_id=None,
                 modules=None, refs_to=None):
        new = Schema.__new__(Schema)

        if modules is None:
            new._modules = self._modules
        else:
            new._modules = modules

        if id_to_data is None:
            new._id_to_data = self._id_to_data
        else:
            new._id_to_data = id_to_data

        if id_to_type is None:
            new._id_to_type = self._id_to_type
        else:
            new._id_to_type = id_to_type

        if name_to_id is None:
            new._name_to_id = self._name_to_id
        else:
            new._name_to_id = name_to_id

        if shortname_to_id is None:
            new._shortname_to_id = self._shortname_to_id
        else:
            new._shortname_to_id = shortname_to_id

        if refs_to is None:
            new._refs_to = self._refs_to
        else:
            new._refs_to = refs_to

        new._generation = self._generation + 1

        return new

    def _update_obj_name(self, obj_id, scls, old_name, new_name):
        name_to_id = self._name_to_id
        shortname_to_id = self._shortname_to_id
        stype = type(scls)

        has_sn_cache = issubclass(stype, (s_func.Function, s_oper.Operator))

        if old_name is not None:
            name_to_id = name_to_id.delete(old_name)
            if has_sn_cache:
                old_shortname = sn.shortname_from_fullname(old_name)
                sn_key = (stype, old_shortname)

                new_ids = shortname_to_id[sn_key] - {obj_id}
                if new_ids:
                    shortname_to_id = shortname_to_id.set(
                        sn_key, new_ids)
                else:
                    shortname_to_id = shortname_to_id.delete(sn_key)

        if new_name is not None:
            if new_name in name_to_id:
                raise s_err.SchemaError(
                    f'name {new_name!r} is already in the schema')
            name_to_id = name_to_id.set(new_name, obj_id)
            if has_sn_cache:
                new_shortname = sn.shortname_from_fullname(new_name)
                sn_key = (stype, new_shortname)

                try:
                    ids = shortname_to_id[sn_key]
                except KeyError:
                    ids = frozenset()

                shortname_to_id = shortname_to_id.set(
                    sn_key, ids | {obj_id})

        return name_to_id, shortname_to_id

    def _update_obj(self, obj_id, updates):
        if not updates:
            return self

        try:
            data = self._id_to_data[obj_id]
        except KeyError:
            data = immu.Map()

        name_to_id = None
        shortname_to_id = None
        with data.mutate() as mm:
            for field, value in updates.items():
                if field == 'name':
                    name_to_id, shortname_to_id = self._update_obj_name(
                        obj_id,
                        self._id_to_type[obj_id],
                        mm.get('name'),
                        value)

                if value is None:
                    mm.pop(field, None)
                else:
                    mm[field] = value

            new_data = mm.finish()

        id_to_data = self._id_to_data.set(obj_id, new_data)
        scls = self._id_to_type[obj_id]
        refs_to = self._update_refs_to(scls, data, new_data)
        return self._replace(name_to_id=name_to_id,
                             shortname_to_id=shortname_to_id,
                             id_to_data=id_to_data,
                             refs_to=refs_to)

    def _get_obj_field(self, obj_id, field):
        try:
            d = self._id_to_data[obj_id]
        except KeyError:
            err = (f'cannot get {field!r} value: item {str(obj_id)!r} '
                   f'is not present in the schema {self!r}')
            raise s_err.SchemaError(err) from None

        return d.get(field)

    def _set_obj_field(self, obj_id, field, value):
        try:
            data = self._id_to_data[obj_id]
        except KeyError:
            err = (f'cannot set {field!r} value: item {str(obj_id)!r} '
                   f'is not present in the schema {self!r}')
            raise s_err.SchemaError(err) from None

        name_to_id = None
        shortname_to_id = None
        if field == 'name':
            old_name = data.get('name')
            name_to_id, shortname_to_id = self._update_obj_name(
                obj_id,
                self._id_to_type[obj_id],
                old_name,
                value)

        new_data = data.set(field, value)
        id_to_data = self._id_to_data.set(obj_id, new_data)
        scls = self._id_to_type[obj_id]

        if field in data:
            orig_field_data = {field: data[field]}
        else:
            orig_field_data = {}

        refs_to = self._update_refs_to(scls, orig_field_data, {field: value})

        return self._replace(name_to_id=name_to_id,
                             shortname_to_id=shortname_to_id,
                             id_to_data=id_to_data,
                             refs_to=refs_to)

    def _unset_obj_field(self, obj_id, field):
        try:
            data = self._id_to_data[obj_id]
        except KeyError:
            return self

        name_to_id = None
        shortname_to_id = None
        name = data.get('name')
        if field == 'name' and name is not None:
            name_to_id, shortname_to_id = self._update_obj_name(
                obj_id,
                self._id_to_type[obj_id],
                name,
                None)
            new_data = data.delete(field)
        else:
            try:
                new_data = data.delete(field)
            except KeyError:
                return self

        id_to_data = self._id_to_data.set(obj_id, new_data)
        scls = self._id_to_type[obj_id]
        refs_to = self._update_refs_to(scls, {field: data[field]}, None)

        return self._replace(name_to_id=name_to_id,
                             shortname_to_id=shortname_to_id,
                             id_to_data=id_to_data,
                             refs_to=refs_to)

    def _update_refs_to(self, scls, orig_data, new_data) -> immu.Map:
        scls_type = type(scls)
        objfields = scls_type.get_object_fields()
        if not objfields:
            return self._refs_to

        id_set = frozenset((scls.id,))

        with self._refs_to.mutate() as mm:
            for field in objfields:
                if not new_data:
                    ids = None
                else:
                    try:
                        ref = new_data[field.name]
                    except KeyError:
                        ids = None
                    else:
                        if isinstance(ref, so.ObjectCollection):
                            ids = frozenset(ref.ids(self))
                        else:
                            ids = frozenset((ref.id,))

                if not orig_data:
                    orig_ids = None
                else:
                    try:
                        ref = orig_data[field.name]
                    except KeyError:
                        orig_ids = None
                    else:
                        if isinstance(ref, so.ObjectCollection):
                            orig_ids = frozenset(ref.ids(self))
                        else:
                            orig_ids = frozenset((ref.id,))

                if not ids and not orig_ids:
                    continue

                key = (scls_type, field.name)

                if ids and orig_ids:
                    new_ids = ids - orig_ids
                    old_ids = orig_ids - ids
                elif ids:
                    new_ids = ids
                    old_ids = None
                else:
                    new_ids = None
                    old_ids = orig_ids

                if new_ids:
                    for ref_id in new_ids:
                        try:
                            refs = mm[ref_id]
                        except KeyError:
                            mm[ref_id] = immu.Map({key: id_set})
                        else:
                            try:
                                field_refs = refs[key]
                            except KeyError:
                                field_refs = id_set
                            else:
                                field_refs |= id_set
                            mm[ref_id] = refs.set(key, field_refs)

                if old_ids:
                    for ref_id in old_ids:
                        refs = mm[ref_id]
                        field_refs = refs[key]
                        field_refs -= id_set
                        if not field_refs:
                            mm[ref_id] = refs.delete(key)
                        else:
                            mm[ref_id] = refs.set(key, field_refs)

            return mm.finish()

    def _add(self, id, scls, data) -> 'Schema':
        name = data['name']

        if name in self._name_to_id:
            err = f'{name!r} is already present in the schema {self!r}'
            raise s_err.SchemaError(err)

        data = immu.Map(data)

        name_to_id, shortname_to_id = self._update_obj_name(
            id, scls, None, name)

        updates = dict(
            id_to_data=self._id_to_data.set(id, data),
            id_to_type=self._id_to_type.set(id, scls),
            name_to_id=name_to_id,
            shortname_to_id=shortname_to_id,
            refs_to=self._update_refs_to(scls, None, data),
        )

        if isinstance(scls, s_modules.Module):
            updates['modules'] = self._modules.set(name, id)
        elif name.module not in self._modules:
            raise s_err.SchemaModuleNotFoundError(
                f'module {name.module!r} is not in this schema')

        return self._replace(**updates)

    def _delete(self, obj):
        data = self._id_to_data.get(obj.id)
        if data is None:
            raise s_err.ItemNotFoundError(
                f'cannot delete {obj!r}: not in this schema')

        name = data['name']

        updates = {}

        if isinstance(obj, s_modules.Module):
            updates['modules'] = self._modules.delete(name)

        name_to_id, shortname_to_id = self._update_obj_name(
            obj.id, self._id_to_type[obj.id], name, None)

        refs_to = self._update_refs_to(obj, self._id_to_data[obj.id], None)

        updates.update(dict(
            name_to_id=name_to_id,
            shortname_to_id=shortname_to_id,
            id_to_data=self._id_to_data.delete(obj.id),
            id_to_type=self._id_to_type.delete(obj.id),
            refs_to=refs_to,
        ))

        return self._replace(**updates)

    def discard(self, obj) -> 'Schema':
        if obj.id in self._id_to_data:
            return self._delete(obj)
        else:
            return self

    def delete(self, obj) -> 'Schema':
        return self._delete(obj)

    def _get(self, name, *, getter, default, module_aliases):
        name, module, shortname = sn.split_name(name)
        implicit_builtins = module is None

        if module_aliases is not None:
            fq_module = module_aliases.get(module)
            if fq_module is not None:
                module = fq_module

        if module is not None:
            fqname = sn.SchemaName(shortname, module)
            result = getter(self, fqname)
            if result is not None:
                return result
        else:
            # Some items have unqualified names, like modules
            # themselves.
            result = getter(self, shortname)
            if result is not None:
                return result

        if implicit_builtins:
            fqname = sn.SchemaName(shortname, 'std')
            result = getter(self, fqname)
            if result is not None:
                return result

        return default

    def get_functions(self, name, default=_void, *, module_aliases=None):
        funcs = self._get(name,
                          getter=_get_functions,
                          module_aliases=module_aliases,
                          default=default)

        if funcs is not _void:
            return funcs

        raise s_err.ItemNotFoundError(
            f'reference to a non-existent function: {name}')

    def get_operators(self, name, default=_void, *, module_aliases=None):
        funcs = self._get(name,
                          getter=_get_operators,
                          module_aliases=module_aliases,
                          default=default)

        if funcs is not _void:
            return funcs

        raise s_err.ItemNotFoundError(
            f'reference to a non-existent operator: {name}')

    @functools.lru_cache()
    def _get_casts(
            self, stype: s_types.Type, *,
            disposition: str,
            implicit: bool=False,
            assignment: bool=False) -> typing.FrozenSet[s_casts.Cast]:

        all_casts = self.get_referrers(
            stype, scls_type=s_casts.Cast, field_name=disposition)

        casts = []
        for cast in all_casts:
            if implicit and not cast.get_allow_implicit(self):
                continue
            if assignment and not cast.get_allow_assignment(self):
                continue
            casts.append(cast)

        return frozenset(casts)

    def get_casts_to_type(
            self, to_type: s_types.Type, *,
            implicit: bool=False,
            assignment: bool=False) -> typing.FrozenSet[s_casts.Cast]:
        return self._get_casts(to_type, disposition='to_type',
                               implicit=implicit, assignment=assignment)

    def get_casts_from_type(
            self, from_type: s_types.Type, *,
            implicit: bool=False,
            assignment: bool=False) -> typing.FrozenSet[s_casts.Cast]:
        return self._get_casts(from_type, disposition='from_type',
                               implicit=implicit, assignment=assignment)

    @functools.lru_cache()
    def get_referrers(
            self, scls: so.Object, *,
            scls_type: typing.Optional[so.ObjectMeta]=None,
            field_name: typing.Optional[str]=None):

        try:
            refs = self._refs_to[scls.id]
        except KeyError:
            return frozenset()
        else:
            referrers = set()

            if scls_type is not None:
                if field_name is not None:
                    for (st, fn), ids in refs.items():
                        if st is scls_type and fn == field_name:
                            referrers.update(
                                self._id_to_type[objid] for objid in ids)
                else:
                    for (st, _), ids in refs.items():
                        if st is scls_type:
                            referrers.update(
                                self._id_to_type[objid] for objid in ids)
            elif field_name is not None:
                raise ValueError(
                    'get_referrers: field_name cannot be used '
                    'without scls_type')
            else:
                ids = itertools.chain.from_iterable(refs.values())
                referrers.update(self._id_to_type[objid] for objid in ids)

            return frozenset(referrers)

    def get_by_id(self, obj_id, default=_void):
        try:
            return self._id_to_type[obj_id]
        except KeyError:
            if default is _void:
                raise s_err.ItemNotFoundError(
                    f'reference to a non-existent schema item: {obj_id}'
                    f' in schema {self!r}'
                ) from None
            else:
                return default

    def _get_by_name(self, name, *, type=None):
        if isinstance(type, tuple):
            for typ in type:
                scls = self._get_by_name(name, type=typ)
                if scls is not None:
                    return scls
            return None

        obj_id = self._name_to_id.get(name)
        if obj_id is None:
            return None

        obj = self._id_to_type[obj_id]
        if type is not None and not isinstance(obj, type):
            return None

        return obj

    def get(self, name, default=_void, *, module_aliases=None, type=None):
        def getter(schema, name):
            return schema._get_by_name(name, type=type)

        obj = self._get(name,
                        getter=getter,
                        module_aliases=module_aliases,
                        default=default)

        if obj is not _void:
            return obj

        raise s_err.ItemNotFoundError(
            f'reference to a non-existent schema item: {name}')

    def has_module(self, module):
        return module in self._modules

    def _get_descendants(self, scls, *, max_depth=None, depth=0):
        result = set()

        _virtual_children = scls.get__virtual_children(self)

        if _virtual_children is None:
            child_names = self._find_children(scls)
        else:
            child_names = [c.material_type(self).get_name(self)
                           for c in _virtual_children.objects(self)]

        children = {self.get(n, type=type(scls)) for n in child_names}

        if max_depth is not None and depth < max_depth:
            for child in children:
                result.update(self._get_descendants(
                    child, max_depth=max_depth, depth=depth + 1))

        result.update(children)
        return result

    def _find_children(self, scls):
        flt = lambda p: scls in p.get_bases(self).objects(self)
        it = self.get_objects(type=type(scls))
        return {c.get_name(self) for c in filter(flt, it)}

    def get_objects(self, *, modules=None, type=None):
        return SchemaIterator(self, modules=modules, type=type)

    def __repr__(self):
        return (
            f'<{type(self).__name__} gen:{self._generation} at {id(self):#x}>')


class SchemaIterator:
    def __init__(
            self,
            schema, *,
            modules: typing.Optional[typing.Iterable[str]],
            type=None) -> None:

        filters = []

        if type is not None:
            filters.append(
                lambda obj: isinstance(obj, type))

        if modules is not None:
            modules = frozenset(modules)
            filters.append(
                lambda obj:
                    not isinstance(obj, s_modules.Module) and
                    obj.get_name(schema).module in modules)

        self._filters = filters
        self._schema = schema

    def __iter__(self):
        filters = self._filters
        index = self._schema._id_to_type

        for obj_id, obj in index.items():
            if all(f(obj) for f in filters):
                yield obj


@functools.lru_cache()
def _get_functions(schema, name):
    objids = schema._shortname_to_id.get((s_func.Function, name))
    if objids is None:
        return
    return tuple(schema._id_to_type[oid] for oid in objids)


@functools.lru_cache()
def _get_operators(schema, name):
    objids = schema._shortname_to_id.get((s_oper.Operator, name))
    if objids is None:
        return
    return tuple(schema._id_to_type[oid] for oid in objids)
