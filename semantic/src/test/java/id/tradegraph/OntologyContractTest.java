package id.tradegraph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import java.util.stream.Collectors;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.io.FileDocumentSource;
import org.semanticweb.owlapi.model.AxiomType;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLClass;
import org.semanticweb.owlapi.model.OWLDataFactory;
import org.semanticweb.owlapi.model.OWLDataProperty;
import org.semanticweb.owlapi.model.OWLNamedIndividual;
import org.semanticweb.owlapi.model.OWLObjectProperty;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.model.OWLOntologyManager;
import org.semanticweb.owlapi.profiles.OWL2DLProfile;
import org.semanticweb.owlapi.profiles.violations.UseOfNonSimplePropertyInCardinalityRestriction;
import org.semanticweb.owlapi.reasoner.InferenceType;
import org.semanticweb.owlapi.reasoner.OWLReasoner;
import org.semanticweb.owlapi.util.DLExpressivityChecker;
import org.semanticweb.owlapi.vocab.OWL2Datatype;

class OntologyContractTest {
    private static final String KB_IRI = "http://www.semanticweb.org/tradegraph/kb";
    private static final String SHACL = "http://www.w3.org/ns/shacl#";
    private static final OWLDataFactory OWL = OWLManager.getOWLDataFactory();
    private OWLOntologyManager manager;
    private OWLOntology ontology;

    @BeforeEach
    void loadCanonicalOntology() throws Exception {
        var path = Path.of("..", "knowledge", "tradegraph.ttl").toAbsolutePath().normalize();
        assertTrue(Files.isRegularFile(path), () -> "Missing ontology at " + path);
        manager = OWLManager.createOWLOntologyManager();
        manager.getIRIMappers().add(iri -> {
            throw new AssertionError("Unexpected ontology import " + iri);
        });
        ontology = manager.loadOntologyFromOntologyDocument(
                new FileDocumentSource(path.toFile(), new TurtleDocumentFormat()));
        // The standalone Java suite reads canonical Turtle. SHACL is handled in the RDF projection.
        var shapeAnnotations = ontology.getAxioms(AxiomType.ANNOTATION_ASSERTION).stream()
                .filter(axiom -> axiom.getProperty().getIRI().toString().startsWith(SHACL))
                .collect(Collectors.toSet());
        var shapeTypes = ontology.getAxioms(AxiomType.CLASS_ASSERTION).stream()
                .filter(axiom -> axiom.getClassExpression().getSignature().stream()
                        .anyMatch(entity -> entity.getIRI().toString().startsWith(SHACL)))
                .collect(Collectors.toSet());
        manager.removeAxioms(ontology, shapeAnnotations);
        manager.removeAxioms(ontology, shapeTypes);
        manager.removeAxioms(ontology, ontology.getAxioms(AxiomType.SWRL_RULE));
    }

    @Test
    void checksOwlDlConsistencyAndSatisfiability() {
        assertEquals(IRI.create(KB_IRI), ontology.getOntologyID().getOntologyIRI().get());
        assertTrue(ontology.getOntologyID().getVersionIRI().isPresent());
        assertTrue(ontology.getImportsDeclarations().isEmpty());
        var report = new OWL2DLProfile().checkOntology(ontology);
        assertTrue(report.isInProfile(), report::toString);
        withReasoner(reasoner -> {
            assertTrue(reasoner.isConsistent());
            reasoner.precomputeInferences(InferenceType.CLASS_HIERARCHY);
            for (var type : ontology.getClassesInSignature()) {
                if (!type.isOWLNothing()) {
                    assertTrue(reasoner.isSatisfiable(type), type::toString);
                }
            }
        });
        System.out.println("OWLAPI expressivity: "
                + new DLExpressivityChecker(Set.of(ontology)).getDescriptionLogicName());
    }

    @Test
    void entailsMaterialChainAndInversesWithoutLegalOrigin() {
        var product = example("product_p");
        var batch = example("batch_b");
        var material = example("material_m");
        var producer = example("organization");
        var assessment = example("origin-assessment");
        manager.addAxioms(ontology, Set.of(
                OWL.getOWLObjectPropertyAssertionAxiom(property("hasBatch"), product, batch),
                OWL.getOWLObjectPropertyAssertionAxiom(property("usesMaterial"), batch, material),
                OWL.getOWLObjectPropertyAssertionAxiom(property("producedBy"), batch, producer),
                OWL.getOWLClassAssertionAxiom(type("OriginAssessment"), assessment)));
        var dependency = OWL.getOWLObjectPropertyAssertionAxiom(
                property("dependsOnMaterial"), product, material);
        assertFalse(ontology.containsAxiom(dependency));

        withReasoner(reasoner -> {
            assertTrue(reasoner.isEntailed(dependency));
            assertTrue(reasoner.isEntailed(
                    OWL.getOWLObjectPropertyAssertionAxiom(property("batchOf"), batch, product)));
            assertTrue(reasoner.isEntailed(
                    OWL.getOWLObjectPropertyAssertionAxiom(property("produces"), producer, batch)));
            for (var subject : Set.of(product, assessment)) {
                assertFalse(reasoner.isEntailed(OWL.getOWLObjectPropertyAssertionAxiom(
                        property("hasLegalOrigin"), subject, individual("thailand"))));
                assertFalse(reasoner.isEntailed(OWL.getOWLNegativeObjectPropertyAssertionAxiom(
                        property("hasLegalOrigin"), subject, individual("thailand"))));
            }
        });
    }

    @Test
    void keepsRolesAndFindingsScopedToTheirContexts() {
        var organization = example("same-organization");
        var importRole = example("import-role");
        var exportRole = example("export-role");
        var importRevision = example("import-revision");
        var exportRevision = example("export-revision");
        var importContext = example("import-assessment");
        var exportContext = example("export-assessment");
        var finding = example("finding");
        manager.addAxioms(ontology, Set.of(
                OWL.getOWLObjectPropertyAssertionAxiom(property("roleActor"), importRole, organization),
                OWL.getOWLObjectPropertyAssertionAxiom(property("roleActor"), exportRole, organization),
                OWL.getOWLObjectPropertyAssertionAxiom(property("hasActorRole"), importRole, individual("roleImporter")),
                OWL.getOWLObjectPropertyAssertionAxiom(property("hasActorRole"), exportRole, individual("roleExporter")),
                OWL.getOWLObjectPropertyAssertionAxiom(property("assignedInRevision"), importRole, importRevision),
                OWL.getOWLObjectPropertyAssertionAxiom(property("assignedInRevision"), exportRole, exportRevision),
                OWL.getOWLObjectPropertyAssertionAxiom(property("assessesRevision"), importContext, importRevision),
                OWL.getOWLObjectPropertyAssertionAxiom(property("assessesRevision"), exportContext, exportRevision),
                OWL.getOWLObjectPropertyAssertionAxiom(property("findingInContext"), finding, importContext),
                OWL.getOWLDifferentIndividualsAxiom(importRevision, exportRevision),
                OWL.getOWLDifferentIndividualsAxiom(importContext, exportContext)));

        withReasoner(reasoner -> {
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(OWL.getOWLClassAssertionAxiom(type("ImporterRoleAssignment"), importRole)));
            assertTrue(reasoner.isEntailed(OWL.getOWLClassAssertionAxiom(type("ExporterRoleAssignment"), exportRole)));
            assertTrue(reasoner.getObjectPropertyValues(organization, property("hasRoleAssignment"))
                    .containsEntity(importRole));
            assertTrue(reasoner.getObjectPropertyValues(organization, property("hasRoleAssignment"))
                    .containsEntity(exportRole));
            assertTrue(reasoner.getObjectPropertyValues(importRevision, property("revisionRoleAssignment"))
                    .containsEntity(importRole));
            assertFalse(reasoner.getObjectPropertyValues(importRevision, property("revisionRoleAssignment"))
                    .containsEntity(exportRole));
            assertFalse(reasoner.getObjectPropertyValues(exportRevision, property("revisionRoleAssignment"))
                    .containsEntity(importRole));
            assertTrue(reasoner.getObjectPropertyValues(importContext, property("hasFinding")).containsEntity(finding));
            assertFalse(reasoner.getObjectPropertyValues(exportContext, property("hasFinding")).containsEntity(finding));

            manager.addAxiom(ontology, OWL.getOWLObjectPropertyAssertionAxiom(
                    property("findingInContext"), finding, exportContext));
            reasoner.flush();
            assertFalse(reasoner.isConsistent(), "A finding cannot belong to two provably distinct contexts");
        });
    }

    @Test
    void derivesTypesFromAllDomainsAndRange() {
        var batch = example("untyped-batch");
        var material = example("untyped-material");
        var audited = OWL.getOWLClass(IRI.create("http://www.semanticweb.org/tradegraph/test#AuditedRecord"));
        manager.addAxioms(ontology, Set.of(
                OWL.getOWLDeclarationAxiom(audited),
                OWL.getOWLObjectPropertyDomainAxiom(property("usesMaterial"), audited),
                OWL.getOWLObjectPropertyAssertionAxiom(property("usesMaterial"), batch, material)));
        assertFalse(ontology.containsAxiom(OWL.getOWLClassAssertionAxiom(type("ProductionBatch"), batch)));
        assertFalse(ontology.containsAxiom(OWL.getOWLClassAssertionAxiom(type("Material"), material)));
        withReasoner(reasoner -> {
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(OWL.getOWLClassAssertionAxiom(type("ProductionBatch"), batch)));
            assertTrue(reasoner.isEntailed(OWL.getOWLClassAssertionAxiom(audited, batch)));
            assertTrue(reasoner.isEntailed(OWL.getOWLClassAssertionAxiom(type("Material"), material)));
        });
    }

    @Test
    void requiresProvableDistinctnessForMaterialCardinality() {
        var batch = example("counted-batch");
        var first = example("material-a");
        var second = example("material-b");
        var classification = OWL.getOWLClassAssertionAxiom(type("MultiMaterialBatch"), batch);
        var distinct = OWL.getOWLDifferentIndividualsAxiom(first, second);
        var equal = OWL.getOWLSameIndividualAxiom(first, second);
        manager.addAxioms(ontology, Set.of(
                OWL.getOWLObjectPropertyAssertionAxiom(property("usesMaterial"), batch, first),
                OWL.getOWLObjectPropertyAssertionAxiom(property("usesMaterial"), batch, second)));

        withReasoner(reasoner -> {
            assertTrue(reasoner.isConsistent());
            assertFalse(reasoner.isEntailed(distinct));
            assertFalse(reasoner.isEntailed(equal));
            assertFalse(reasoner.isEntailed(classification));

            manager.addAxiom(ontology, equal);
            reasoner.flush();
            assertTrue(reasoner.isConsistent());
            assertFalse(reasoner.isEntailed(classification));

            manager.removeAxiom(ontology, equal);
            manager.addAxiom(ontology, distinct);
            reasoner.flush();
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(classification));
        });
    }

    @Test
    void entailsEqualityForSingleCaseRevisionAndDetectsConflict() {
        var revision = example("revision");
        var first = example("case");
        var alias = example("case-alias");
        manager.addAxioms(ontology, Set.of(
                OWL.getOWLObjectPropertyAssertionAxiom(property("revisionOf"), revision, first),
                OWL.getOWLObjectPropertyAssertionAxiom(property("revisionOf"), revision, alias)));

        withReasoner(reasoner -> {
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(OWL.getOWLSameIndividualAxiom(first, alias)));
            manager.addAxiom(ontology, OWL.getOWLDifferentIndividualsAxiom(first, alias));
            reasoner.flush();
            assertFalse(reasoner.isConsistent());
        });
    }

    @Test
    void rejectsChainSuperpropertyInCardinality() {
        var restriction = OWL.getOWLObjectMinCardinality(2, property("dependsOnMaterial"), type("Material"));
        manager.addAxiom(ontology, OWL.getOWLSubClassOfAxiom(type("Product"), restriction));
        var report = new OWL2DLProfile().checkOntology(ontology);
        assertTrue(report.getViolations().stream()
                .anyMatch(violation -> violation instanceof UseOfNonSimplePropertyInCardinalityRestriction),
                report::toString);
    }

    @Test
    void existentialDoesNotSupplyNamedEvidence() {
        var claim = example("claim");
        var document = example("evidence-document");
        manager.addAxioms(ontology, Set.of(
                OWL.getOWLClassAssertionAxiom(type("DocumentLinkedClaim"), claim),
                OWL.getOWLClassAssertionAxiom(type("Document"), document)));
        var exists = OWL.getOWLObjectSomeValuesFrom(property("claimDocument"), type("Document"));
        var namedLink = OWL.getOWLObjectPropertyAssertionAxiom(property("claimDocument"), claim, document);

        withReasoner(reasoner -> {
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(OWL.getOWLClassAssertionAxiom(exists, claim)));
            assertTrue(reasoner.getObjectPropertyValues(claim, property("claimDocument")).isEmpty(),
                    "An existential witness does not discharge named-document validation");
            assertFalse(reasoner.isEntailed(namedLink));

            manager.addAxiom(ontology, namedLink);
            reasoner.flush();
            assertTrue(reasoner.getObjectPropertyValues(claim, property("claimDocument")).containsEntity(document));
        });
    }

    @Test
    void derivesNestedLocationAndTypesAssessmentInstants() {
        var facility = example("warehouse");
        var port = example("port");
        var assessment = example("assessment");
        var timestamp = OWL.getOWLLiteral("2025-09-15T08:00:00Z", OWL2Datatype.XSD_DATE_TIME);
        var revision = ontology.getAnnotations().stream()
                .filter(annotation -> annotation.getProperty().getIRI().equals(iri("currentRevision")))
                .findFirst().orElseThrow().getValue();
        assertTrue(revision instanceof IRI);
        manager.addAxioms(ontology, Set.of(
                OWL.getOWLObjectPropertyAssertionAxiom(property("withinLocation"), facility, port),
                OWL.getOWLObjectPropertyAssertionAxiom(property("withinLocation"), port, individual("thailand")),
                OWL.getOWLDataPropertyAssertionAxiom(dataProperty("transactionAt"), assessment, timestamp),
                OWL.getOWLObjectPropertyAssertionAxiom(property("usesKnowledgeRevision"), assessment,
                        OWL.getOWLNamedIndividual((IRI) revision))));

        withReasoner(reasoner -> {
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(OWL.getOWLObjectPropertyAssertionAxiom(
                    property("withinLocation"), facility, individual("thailand"))));
            assertTrue(reasoner.isEntailed(OWL.getOWLClassAssertionAxiom(type("AssessmentContext"), assessment)));
            assertTrue(reasoner.isEntailed(OWL.getOWLDataPropertyAssertionAxiom(
                    dataProperty("transactionAt"), assessment, timestamp)));
        });
    }

    @Test
    void retainsTariffAxiomProvenanceAndInitialCoverage() {
        var axiomSource = OWL.getOWLAnnotationProperty(iri("axiomSource"));
        var axiomId = OWL.getOWLAnnotationProperty(iri("axiomId"));
        var reviewStatus = OWL.getOWLAnnotationProperty(iri("axiomReviewStatus"));
        var expectedClauses = Map.of(
                "8413.70.11", "tariff-row-7571",
                "8413.91.30", "tariff-row-7590",
                "0901.21.11", "tariff-row-883");
        var tariffSource = individual("source-id-tarif-atiga");
        for (var entry : expectedClauses.entrySet()) {
            var code = individual("hs-" + entry.getKey().replace(".", ""));
            var reference = ontology.getDataPropertyAssertionAxioms(code).stream()
                    .filter(axiom -> axiom.getProperty().equals(dataProperty("codeValue")))
                    .findFirst().orElseThrow();
            assertEquals(entry.getKey(), reference.getObject().getLiteral());
            var referenceId = OWL.getOWLLiteral("ref-" + code.getIRI().getRemainder().get());
            assertTrue(reference.getAnnotations().contains(OWL.getOWLAnnotation(axiomId, referenceId)));
            assertTrue(reference.getAnnotations().contains(
                    OWL.getOWLAnnotation(axiomSource, iri(entry.getValue()))));
            assertTrue(reference.getAnnotations().contains(
                    OWL.getOWLAnnotation(reviewStatus, OWL.getOWLLiteral("SOURCE_REVIEWED"))));
            assertTrue(ontology.containsAxiom(OWL.getOWLObjectPropertyAssertionAxiom(
                    property("clauseInDocument"), individual(entry.getValue()), tariffSource)));
        }
        assertTrue(ontology.getDataPropertyAssertionAxioms(tariffSource).stream()
                .anyMatch(axiom -> axiom.getProperty().equals(dataProperty("sourceRecordId"))
                        && axiom.getObject().getLiteral().matches("source-[0-9a-f]{64}")));
        for (var scope : Set.of("scope-pump-import", "scope-pump-exhibition", "scope-coffee-export")) {
            assertTrue(ontology.containsAxiom(OWL.getOWLObjectPropertyAssertionAxiom(
                    property("hasCoverageStatus"), individual(scope), individual("coveragePartial"))));
        }
    }

    private void withReasoner(Consumer<OWLReasoner> check) {
        var reasoner = new ReasonerFactory().createReasoner(ontology);
        try {
            check.accept(reasoner);
        } finally {
            reasoner.dispose();
        }
    }

    private static IRI iri(String localName) {
        return IRI.create(KB_IRI + "#" + localName);
    }

    private static OWLNamedIndividual individual(String localName) {
        return OWL.getOWLNamedIndividual(iri(localName));
    }

    private static OWLNamedIndividual example(String localName) {
        return OWL.getOWLNamedIndividual(IRI.create("http://www.semanticweb.org/tradegraph/test#" + localName));
    }

    private static OWLObjectProperty property(String localName) {
        return OWL.getOWLObjectProperty(iri(localName));
    }

    private static OWLDataProperty dataProperty(String localName) {
        return OWL.getOWLDataProperty(iri(localName));
    }

    private static OWLClass type(String localName) {
        return OWL.getOWLClass(iri(localName));
    }
}
