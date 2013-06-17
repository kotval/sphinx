# -*- coding: utf-8 -*-
"""
    sphinx.transforms
    ~~~~~~~~~~~~~~~~~

    Docutils transforms used by Sphinx when reading documents.

    :copyright: Copyright 2007-2013 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

from os import path

from docutils import nodes
from docutils.utils import new_document, relative_path
from docutils.parsers.rst import Parser as RSTParser
from docutils.transforms import Transform
from docutils.transforms.parts import ContentsFilter

from sphinx import addnodes
from sphinx.locale import _, init as init_locale
from sphinx.util import split_index_msg
from sphinx.util.nodes import traverse_translatable_index, extract_messages
from sphinx.util.osutil import ustrftime, find_catalog
from sphinx.util.compat import docutils_version
from sphinx.util.pycompat import all
from sphinx.domains.std import (
    make_term_from_paragraph_node,
    make_termnodes_from_paragraph_node,
)


default_substitutions = set([
    'version',
    'release',
    'today',
])

class DefaultSubstitutions(Transform):
    """
    Replace some substitutions if they aren't defined in the document.
    """
    # run before the default Substitutions
    default_priority = 210

    def apply(self):
        config = self.document.settings.env.config
        # only handle those not otherwise defined in the document
        to_handle = default_substitutions - set(self.document.substitution_defs)
        for ref in self.document.traverse(nodes.substitution_reference):
            refname = ref['refname']
            if refname in to_handle:
                text = config[refname]
                if refname == 'today' and not text:
                    # special handling: can also specify a strftime format
                    text = ustrftime(config.today_fmt or _('%B %d, %Y'))
                ref.replace_self(nodes.Text(text, text))


class MoveModuleTargets(Transform):
    """
    Move module targets that are the first thing in a section to the section
    title.

    XXX Python specific
    """
    default_priority = 210

    def apply(self):
        for node in self.document.traverse(nodes.target):
            if not node['ids']:
                continue
            if (node.has_key('ismod') and
                node.parent.__class__ is nodes.section and
                # index 0 is the section title node
                node.parent.index(node) == 1):
                node.parent['ids'][0:0] = node['ids']
                node.parent.remove(node)


class HandleCodeBlocks(Transform):
    """
    Several code block related transformations.
    """
    default_priority = 210

    def apply(self):
        # move doctest blocks out of blockquotes
        for node in self.document.traverse(nodes.block_quote):
            if all(isinstance(child, nodes.doctest_block) for child
                     in node.children):
                node.replace_self(node.children)
        # combine successive doctest blocks
        #for node in self.document.traverse(nodes.doctest_block):
        #    if node not in node.parent.children:
        #        continue
        #    parindex = node.parent.index(node)
        #    while len(node.parent) > parindex+1 and \
        #            isinstance(node.parent[parindex+1], nodes.doctest_block):
        #        node[0] = nodes.Text(node[0] + '\n\n' +
        #                             node.parent[parindex+1][0])
        #        del node.parent[parindex+1]


class SortIds(Transform):
    """
    Sort secion IDs so that the "id[0-9]+" one comes last.
    """
    default_priority = 261

    def apply(self):
        for node in self.document.traverse(nodes.section):
            if len(node['ids']) > 1 and node['ids'][0].startswith('id'):
                node['ids'] = node['ids'][1:] + [node['ids'][0]]


class CitationReferences(Transform):
    """
    Replace citation references by pending_xref nodes before the default
    docutils transform tries to resolve them.
    """
    default_priority = 619

    def apply(self):
        for citnode in self.document.traverse(nodes.citation_reference):
            cittext = citnode.astext()
            refnode = addnodes.pending_xref(cittext, reftype='citation',
                                            reftarget=cittext, refwarn=True,
                                            ids=citnode["ids"])
            refnode.line = citnode.line or citnode.parent.line
            refnode += nodes.Text('[' + cittext + ']')
            citnode.parent.replace(citnode, refnode)


class CustomLocaleReporter(object):
    """
    Replacer for document.reporter.get_source_and_line method.

    reST text lines for translation do not have the original source line number.
    This class provides the correct line numbers when reporting.
    """
    def __init__(self, source, line):
        self.source, self.line = source, line

    def set_reporter(self, document):
        if docutils_version < (0, 9):
            document.reporter.locator = self.get_source_and_line
        else:
            document.reporter.get_source_and_line = self.get_source_and_line

    def get_source_and_line(self, lineno=None):
        return self.source, self.line


class Locale(Transform):
    """
    Replace translatable nodes with their translated doctree.
    """
    default_priority = 0

    def apply(self):
        env = self.document.settings.env
        settings, source = self.document.settings, self.document['source']
        # XXX check if this is reliable
        assert source.startswith(env.srcdir)
        docname = path.splitext(relative_path(env.srcdir, source))[0]
        textdomain = find_catalog(docname,
                                  self.document.settings.gettext_compact)

        # fetch translations
        dirs = [path.join(env.srcdir, directory)
                for directory in env.config.locale_dirs]
        catalog, has_catalog = init_locale(dirs, env.config.language,
                                           textdomain)
        if not has_catalog:
            return

        parser = RSTParser()

        for node, msg in extract_messages(self.document):
            msgstr = catalog.gettext(msg)
            # XXX add marker to untranslated parts
            if not msgstr or msgstr == msg: # as-of-yet untranslated
                continue

            # Avoid "Literal block expected; none found." warnings.
            # If msgstr ends with '::' then it cause warning message at
            # parser.parse() processing.
            # literal-block-warning is only appear in avobe case.
            if msgstr.strip().endswith('::'):
                msgstr += '\n\n   dummy literal'
                # dummy literal node will discard by 'patch = patch[0]'

            patch = new_document(source, settings)
            CustomLocaleReporter(node.source, node.line).set_reporter(patch)
            parser.parse(msgstr, patch)
            patch = patch[0]
            # XXX doctest and other block markup
            if not isinstance(patch, nodes.paragraph):
                continue # skip for now

            # update title(section) target name-id mapping
            if isinstance(node, nodes.title):
                section_node = node.parent
                new_name = nodes.fully_normalize_name(patch.astext())
                old_name = nodes.fully_normalize_name(node.astext())

                if old_name != new_name:
                    # if name would be changed, replace node names and
                    # document nameids mapping with new name.
                    names = section_node.setdefault('names', [])
                    names.append(new_name)
                    if old_name in names:
                        names.remove(old_name)

                    id = self.document.nameids.pop(old_name)
                    self.document.set_name_id_map(
                            section_node, id, section_node, explicit=None)

            # auto-numbered foot note reference should use original 'ids'.
            def is_autonumber_footnote_ref(node):
                return isinstance(node, nodes.footnote_reference) and \
                    node.get('auto') == 1
            old_foot_refs = node.traverse(is_autonumber_footnote_ref)
            new_foot_refs = patch.traverse(is_autonumber_footnote_ref)
            if len(old_foot_refs) != len(new_foot_refs):
                env.warn_node('inconsistent footnote references in '
                              'translated message', node)
            for old, new in zip(old_foot_refs, new_foot_refs):
                new['ids'] = old['ids']
                for id in new['ids']:
                    self.document.ids[id] = new
                self.document.autofootnote_refs.remove(old)
                self.document.note_autofootnote_ref(new)

            # reference should use new (translated) 'refname'.
            # * reference target ".. _Python: ..." is not translatable.
            # * use translated refname for section refname.
            # * inline reference "`Python <...>`_" has no 'refname'.
            def is_refnamed_ref(node):
                return isinstance(node, nodes.reference) and  \
                    'refname' in node
            old_refs = node.traverse(is_refnamed_ref)
            new_refs = patch.traverse(is_refnamed_ref)
            if len(old_refs) != len(new_refs):
                env.warn_node('inconsistent references in '
                              'translated message', node)
            for new in new_refs:
                self.document.note_refname(new)

            # refnamed footnote and citation should use original 'ids'.
            def is_refnamed_footnote_ref(node):
                footnote_ref_classes = (nodes.footnote_reference,
                                        nodes.citation_reference)
                return isinstance(node, footnote_ref_classes) and \
                    'refname' in node
            old_refs = node.traverse(is_refnamed_footnote_ref)
            new_refs = patch.traverse(is_refnamed_footnote_ref)
            refname_ids_map = {}
            if len(old_refs) != len(new_refs):
                env.warn_node('inconsistent references in '
                              'translated message', node)
            for old in old_refs:
                refname_ids_map[old["refname"]] = old["ids"]
            for new in new_refs:
                refname = new["refname"]
                if refname in refname_ids_map:
                    new["ids"] = refname_ids_map[refname]

            # glossary terms update refid
            if isinstance(node, nodes.term):
                new_id, _, termnodes = \
                    make_termnodes_from_paragraph_node(env, patch)
                term = make_term_from_paragraph_node(
                        termnodes, [new_id])
                patch = term
                node['ids'] = patch['ids']
                node['names'] = patch['names']

            # Original pending_xref['reftarget'] contain not-translated
            # target name, new pending_xref must use original one.
            # This code restricts to change ref-targets in the translation.
            old_refs = node.traverse(addnodes.pending_xref)
            new_refs = patch.traverse(addnodes.pending_xref)
            xref_reftarget_map = {}
            if len(old_refs) != len(new_refs):
                env.warn_node('inconsistent term references in '
                              'translated message', node)
            def get_ref_key(node):
                case = node["refdomain"], node["reftype"]
                if case == ('std', 'term'):
                    return None
                else:
                    return (
                        node["refdomain"],
                        node["reftype"],
                        node['reftarget'],)

            for old in old_refs:
                key = get_ref_key(old)
                if key:
                    xref_reftarget_map[key] = old["reftarget"]
            for new in new_refs:
                key = get_ref_key(new)
                if key in xref_reftarget_map:
                    new['reftarget'] = xref_reftarget_map[key]

            # update leaves
            for child in patch.children:
                child.parent = node
            node.children = patch.children

        # Extract and translate messages for index entries.
        for node, entries in traverse_translatable_index(self.document):
            new_entries = []
            for type, msg, tid, main in entries:
                msg_parts = split_index_msg(type, msg)
                msgstr_parts = []
                for part in msg_parts:
                    msgstr = catalog.gettext(part)
                    if not msgstr:
                        msgstr = part
                    msgstr_parts.append(msgstr)

                new_entries.append((type, ';'.join(msgstr_parts), tid, main))

            node['raw_entries'] = entries
            node['entries'] = new_entries


class RemoveTranslatableInline(Transform):
    """
    Remove inline nodes used for translation as placeholders.
    """
    default_priority = 999

    def apply(self):
        from sphinx.builders.gettext import MessageCatalogBuilder
        env = self.document.settings.env
        builder = env.app.builder
        if isinstance(builder, MessageCatalogBuilder):
            return
        for inline in self.document.traverse(nodes.inline):
            if 'translatable' in inline:
                inline.parent.remove(inline)
                inline.parent += inline.children


class SphinxContentsFilter(ContentsFilter):
    """
    Used with BuildEnvironment.add_toc_from() to discard cross-file links
    within table-of-contents link nodes.
    """
    def visit_pending_xref(self, node):
        text = node.astext()
        self.parent.append(nodes.literal(text, text))
        raise nodes.SkipNode

    def visit_image(self, node):
        raise nodes.SkipNode
