package id.tradegraph;

import java.util.List;

import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLAxiom;
import org.semanticweb.owlapi.model.OWLClassAssertionAxiom;
import org.semanticweb.owlapi.model.OWLDataFactory;
import org.semanticweb.owlapi.model.OWLDataPropertyAssertionAxiom;
import org.semanticweb.owlapi.model.OWLIndividualAxiom;
import org.semanticweb.owlapi.model.OWLObjectPropertyAssertionAxiom;
import org.semanticweb.owlapi.vocab.OWLRDFVocabulary;

public record InferenceResult(
        String executionStatus,
        String consistency,
        ReasoningJob.Context context,
        List<Fact> inferredFacts,
        String completenessClaim,
        List<String> diagnostics) {
    public record Fact(
            String subject, String predicate, String value, String datatype, String language,
            String kind, int iteration) {
        static Fact from(OWLIndividualAxiom axiom, String kind, int iteration) {
            if (axiom instanceof OWLClassAssertionAxiom assertion) {
                return new Fact(
                        assertion.getIndividual().asOWLNamedIndividual().getIRI().toString(),
                        OWLRDFVocabulary.RDF_TYPE.getIRI().toString(),
                        assertion.getClassExpression().asOWLClass().getIRI().toString(),
                        null, null, kind, iteration);
            }
            if (axiom instanceof OWLObjectPropertyAssertionAxiom assertion) {
                return new Fact(
                        assertion.getSubject().asOWLNamedIndividual().getIRI().toString(),
                        assertion.getProperty().getNamedProperty().getIRI().toString(),
                        assertion.getObject().asOWLNamedIndividual().getIRI().toString(),
                        null, null, kind, iteration);
            }
            var assertion = (OWLDataPropertyAssertionAxiom) axiom;
            var value = assertion.getObject();
            return new Fact(
                    assertion.getSubject().asOWLNamedIndividual().getIRI().toString(),
                    assertion.getProperty().asOWLDataProperty().getIRI().toString(),
                    value.getLiteral(), value.getDatatype().getIRI().toString(),
                    value.hasLang() ? value.getLang() : null, kind, iteration);
        }

        static Fact namedAssertion(OWLAxiom axiom) {
            return switch (axiom) {
                case OWLClassAssertionAxiom a when !a.getIndividual().isAnonymous()
                        && !a.getClassExpression().isAnonymous() -> from(a, "PREMISE", 0);
                case OWLObjectPropertyAssertionAxiom a when !a.getSubject().isAnonymous()
                        && !a.getObject().isAnonymous() && !a.getProperty().isAnonymous() -> from(a, "PREMISE", 0);
                case OWLDataPropertyAssertionAxiom a when !a.getSubject().isAnonymous() -> from(a, "PREMISE", 0);
                default -> null;
            };
        }

        OWLIndividualAxiom toAssertion(OWLDataFactory factory) {
            var individual = factory.getOWLNamedIndividual(IRI.create(subject));
            var property = IRI.create(predicate);
            if (property.equals(OWLRDFVocabulary.RDF_TYPE.getIRI())) {
                return factory.getOWLClassAssertionAxiom(factory.getOWLClass(IRI.create(value)), individual);
            }
            if (language != null || datatype != null) {
                var literal = language != null
                        ? factory.getOWLLiteral(value, language)
                        : factory.getOWLLiteral(value, factory.getOWLDatatype(IRI.create(datatype)));
                return factory.getOWLDataPropertyAssertionAxiom(factory.getOWLDataProperty(property),
                        individual, literal);
            }
            return factory.getOWLObjectPropertyAssertionAxiom(factory.getOWLObjectProperty(property),
                    individual, factory.getOWLNamedIndividual(IRI.create(value)));
        }
    }
}
