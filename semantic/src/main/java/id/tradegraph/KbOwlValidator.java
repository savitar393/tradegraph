package id.tradegraph;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Base64;
import java.util.HashSet;

import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.io.FileDocumentSource;
import org.semanticweb.owlapi.model.AxiomType;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLLiteral;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.profiles.OWL2DLProfile;
import org.semanticweb.owlapi.reasoner.InferenceType;

/** Check the RDF projections or replay an OWLAPI Turtle save for round-trip verification. */
public final class KbOwlValidator {
    private static final IRI RULE_ID = IRI.create("urn:tradegraph:kb#ruleId");

    public static void main(String[] args) throws Exception {
        var core = load(args[0]);
        if (args.length > 2 && args[2] != null && !args[2].isEmpty()) {
            core.getOWLOntologyManager().saveOntology(core, new TurtleDocumentFormat(),
                    IRI.create(Path.of(args[2]).toFile()));
            return;
        }
        var report = new OWL2DLProfile().checkOntology(core);
        if (!report.isInProfile()) {
            throw new IllegalArgumentException("OWL 2 DL profile violations\n" + report);
        }
        var reasoner = new ReasonerFactory().createReasoner(core);
        try {
            if (!reasoner.isConsistent()) {
                throw new IllegalArgumentException("OWL core is inconsistent");
            }
            reasoner.precomputeInferences(InferenceType.CLASS_HIERARCHY);
            var unsatisfiable = reasoner.getUnsatisfiableClasses().getEntitiesMinusBottom();
            if (!unsatisfiable.isEmpty()) {
                throw new IllegalArgumentException("Unsatisfiable classes " + unsatisfiable);
            }
        } finally {
            reasoner.dispose();
        }

        if (args.length > 1 && args[1] != null && !args[1].isEmpty()) {
            reportRules(load(args[1]));
        }
        System.out.println("TRADEGRAPH_OWL_VALID");
    }

    static OWLOntology load(String path) throws Exception {
        var manager = OWLManager.createOWLOntologyManager();
        manager.getIRIMappers().add(iri -> {
            throw new IllegalArgumentException("Unexpected ontology import " + iri);
        });
        return manager.loadOntologyFromOntologyDocument(
                new FileDocumentSource(Path.of(path).toFile(), new TurtleDocumentFormat()));
    }

    private static void reportRules(OWLOntology ontology) {
        var ruleId = ontology.getOWLOntologyManager().getOWLDataFactory().getOWLAnnotationProperty(RULE_ID);
        var seen = new HashSet<String>();
        for (var rule : ontology.getAxioms(AxiomType.SWRL_RULE)) {
            var ids = rule.getAnnotations(ruleId);
            if (ids.size() != 1 || !(ids.iterator().next().getValue() instanceof OWLLiteral literal)
                    || !seen.add(literal.getLiteral())) {
                throw new IllegalArgumentException("Expected one unique literal SWRL ruleId");
            }
            var idBytes = literal.getLiteral().getBytes(StandardCharsets.UTF_8);
            var encodedId = Base64.getEncoder().encodeToString(idBytes);
            System.out.printf("TRADEGRAPH_SWRL_RULE\t%s\t%d\t%d%n",
                    encodedId, rule.getBody().size(), rule.getHead().size());
        }
    }
}
