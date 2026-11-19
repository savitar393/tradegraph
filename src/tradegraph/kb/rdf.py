"""Parse Turtle while retaining the lexical form of typed RDF literals."""

from rdflib import BNode, Graph, Literal, XSD
from rdflib.plugins.parsers.notation3 import (
    RDFSink,
    SinkParser,
    decimal_syntax,
    exponent_syntax,
    integer_syntax,
    numberCharsPlus,
)


class LexicalRDFSink(RDFSink):
    def newLiteral(self, s, dt, lang):
        datatype = dt if dt is not None or lang else XSD.string
        return Literal(s, datatype=datatype, lang=lang, normalize=False)


class LexicalTurtleParser(SinkParser):
    def nodeOrLiteral(self, text, start, result):
        position = self.skipSpace(text, start)
        if 0 <= position < len(text) and text[position] in numberCharsPlus:
            for pattern, datatype in (
                (exponent_syntax, XSD.double),
                (decimal_syntax, XSD.decimal),
                (integer_syntax, XSD.integer),
            ):
                match = pattern.match(text, position)
                if match is not None:
                    result.append(Literal(match.group(), datatype=datatype, normalize=False))
                    return match.end()
        return super().nodeOrLiteral(text, start, result)


class PersistedTurtleParser(LexicalTurtleParser):
    def anonymousNode(self, label):
        return BNode(label)


def parse_turtle(content: bytes, base_iri: str, *, preserve_bnode_labels: bool = False) -> Graph:
    # Turtle numeric shorthand also names an RDF lexical form (for example, +01).
    # Using a per-parse sink avoids changing RDFLib's process-wide literal setting.
    graph = Graph()
    parser_type = PersistedTurtleParser if preserve_bnode_labels else LexicalTurtleParser
    parser = parser_type(LexicalRDFSink(graph), baseURI=base_iri, turtle=True)
    parser.loadBuf(content)
    for prefix, namespace in parser._bindings.items():
        graph.bind(prefix, namespace)
    return graph
